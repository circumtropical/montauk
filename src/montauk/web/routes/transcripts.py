"""Transcripts: import a WhatsApp .txt export, map its participants to
people, and run the extraction model over the new messages (spec 14-17)."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile as StarletteUploadFile

from ...db import models as orm
from ...db.crypto import SecretBox
from ...db.repositories import Actor, PeopleRepository, WorkspaceScope
from ...services import extraction, model_config, transcripts
from ...services.auth import AuthContext
from ...services.transcripts import TranscriptError
from ..app import TEMPLATES, AppState, db_session, get_state
from ..deps import csrf_protect, page_context, require_auth, workspace_scope

logger = logging.getLogger("montauk.transcripts")

router = APIRouter(prefix="/transcripts")

_MAX_UPLOAD = 12 * 1024 * 1024


def _ctx(request: Request, auth: AuthContext, session: Session, **extra: object) -> dict:
    return page_context(
        request,
        auth,
        extraction_configured=model_config.is_configured(session, auth.workspace_id, "extraction"),
        **extra,
    )


@router.get("", response_class=HTMLResponse)
def index(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    session: Session = Depends(db_session),
    scope: WorkspaceScope = Depends(workspace_scope),
    error: str | None = None,
) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request,
        "transcripts.html",
        _ctx(request, auth, session, threads=transcripts.list_threads(session, scope), error=error),
    )


@router.post("/import", dependencies=[Depends(csrf_protect)])
async def do_import(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    session: Session = Depends(db_session),
    scope: WorkspaceScope = Depends(workspace_scope),
) -> Response:
    if not model_config.is_configured(session, auth.workspace_id, "extraction"):
        return _err(request, auth, session, scope, "Configure an extraction model in Settings first.")
    form = await request.form()
    upload = form.get("file")
    if not isinstance(upload, StarletteUploadFile) or not upload.filename:
        return _err(request, auth, session, scope, "Choose a WhatsApp .txt export to import.")
    content = await upload.read()
    if len(content) > _MAX_UPLOAD:
        return _err(request, auth, session, scope, "That file is larger than 12 MB.")
    try:
        result = transcripts.import_whatsapp(
            session,
            scope,
            filename=upload.filename,
            content=content,
            created_by=auth.user_id,
        )
    except TranscriptError as exc:
        return _err(request, auth, session, scope, str(exc))
    return RedirectResponse(f"/transcripts/{result.thread_id}?imported=1", status_code=303)


@router.get("/{thread_id}", response_class=HTMLResponse)
def thread_page(
    thread_id: uuid.UUID,
    request: Request,
    auth: AuthContext = Depends(require_auth),
    session: Session = Depends(db_session),
    scope: WorkspaceScope = Depends(workspace_scope),
    imported: int = 0,
    extracting: int = 0,
    error: str | None = None,
) -> HTMLResponse:
    try:
        view = transcripts.get_thread(session, scope, thread_id)
    except TranscriptError:
        return RedirectResponse("/transcripts", status_code=303)  # type: ignore[return-value]
    people = PeopleRepository(scope).list_people(archived=False, limit=500)
    job = get_state(request).extraction_jobs.get(thread_id) or {}
    return TEMPLATES.TemplateResponse(
        request,
        "transcript_thread.html",
        _ctx(
            request,
            auth,
            session,
            view=view,
            people=people,
            just_imported=bool(imported),
            error=error,
            extraction_active=bool(job.get("active")) or bool(extracting),
            extraction_summary=job.get("summary"),
        ),
    )


@router.post("/{thread_id}/participants", dependencies=[Depends(csrf_protect)])
async def set_mapping(
    thread_id: uuid.UUID,
    request: Request,
    auth: AuthContext = Depends(require_auth),
    session: Session = Depends(db_session),
    scope: WorkspaceScope = Depends(workspace_scope),
) -> Response:
    form = await request.form()
    try:
        transcripts.map_participant(
            session,
            scope,
            thread_id,
            uuid.UUID(str(form.get("participant_id"))),
            role=str(form.get("role", "unmapped")),
            person_public_id=(str(form.get("person_id", "")).strip() or None),
        )
    except (TranscriptError, ValueError) as exc:
        return _thread_err(thread_id, str(exc))
    return RedirectResponse(f"/transcripts/{thread_id}", status_code=303)


def _message_counts(session: Session, thread_id: uuid.UUID) -> dict[str, int]:
    rows: dict[str, int] = {
        status: count
        for status, count in session.execute(
            select(orm.SourceMessage.processing_status, func.count(orm.SourceMessage.id))
            .where(orm.SourceMessage.thread_id == thread_id)
            .group_by(orm.SourceMessage.processing_status)
        ).all()
    }
    processed = rows.get("processed", 0)
    remaining = rows.get("awaiting_processing", 0)
    return {"processed": processed, "remaining": remaining, "total": processed + remaining}


async def _run_extraction_job(
    state: AppState, workspace_id: uuid.UUID, thread_id: uuid.UUID, secret_box: SecretBox | None
) -> None:
    """Background job: extract every pending day for this thread, committing
    per day so the status poll sees live progress. Long-running by design --
    it is not tied to the request that started it."""
    job = state.extraction_jobs[thread_id]
    try:
        with state.session_factory() as session:
            scope = WorkspaceScope(session, workspace_id, Actor("owner", "extraction-job"))
            res = await extraction.run_extraction(
                session, scope, thread_id=thread_id, secret_box=secret_box, max_days=10_000
            )
            session.commit()
        job["summary"] = (
            f"{res.status}: {res.facts_added} fact(s), {res.interactions_touched} interaction(s) "
            f"from {res.days_processed} day(s)" + (f" -- {res.note}" if res.note else "")
        )
    except Exception:  # noqa: BLE001 -- a background job must not crash silently
        logger.exception("extraction job failed for thread %s", thread_id)
        job["summary"] = "Extraction stopped on an unexpected error -- see the logs; click to retry."
    finally:
        job["active"] = False


@router.post("/{thread_id}/extract", dependencies=[Depends(csrf_protect)])
async def do_extract(
    thread_id: uuid.UUID,
    request: Request,
    auth: AuthContext = Depends(require_auth),
    session: Session = Depends(db_session),
    scope: WorkspaceScope = Depends(workspace_scope),
) -> Response:
    try:
        transcripts.get_thread(session, scope, thread_id)
    except TranscriptError:
        return RedirectResponse("/transcripts", status_code=303)

    state = get_state(request)
    job = state.extraction_jobs.get(thread_id)
    if job is None or not job.get("active"):
        state.extraction_jobs[thread_id] = job = {
            "active": True,
            "summary": None,
            "started_at": dt.datetime.now(dt.UTC),
        }
        job["task"] = asyncio.create_task(
            _run_extraction_job(state, auth.workspace_id, thread_id, state.secret_box)
        )
    return RedirectResponse(f"/transcripts/{thread_id}?extracting=1", status_code=303)


@router.get("/{thread_id}/extract/status")
def extract_status(
    thread_id: uuid.UUID,
    request: Request,
    auth: AuthContext = Depends(require_auth),
    session: Session = Depends(db_session),
    scope: WorkspaceScope = Depends(workspace_scope),
) -> JSONResponse:
    try:
        transcripts.get_thread(session, scope, thread_id)
    except TranscriptError:
        return JSONResponse({"error": "not found"}, status_code=404)
    job = get_state(request).extraction_jobs.get(thread_id) or {}
    return JSONResponse(
        {
            **_message_counts(session, thread_id),
            "active": bool(job.get("active")),
            "summary": job.get("summary"),
        }
    )


@router.post("/{thread_id}/delete", dependencies=[Depends(csrf_protect)])
async def delete(
    thread_id: uuid.UUID,
    auth: AuthContext = Depends(require_auth),
    session: Session = Depends(db_session),
    scope: WorkspaceScope = Depends(workspace_scope),
) -> Response:
    try:
        transcripts.delete_thread(session, scope, thread_id)
    except TranscriptError:
        pass
    return RedirectResponse("/transcripts", status_code=303)


def _err(
    request: Request, auth: AuthContext, session: Session, scope: WorkspaceScope, msg: str
) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request,
        "transcripts.html",
        _ctx(request, auth, session, threads=transcripts.list_threads(session, scope), error=msg),
        status_code=400,
    )


def _thread_err(thread_id: uuid.UUID, msg: str) -> RedirectResponse:
    return RedirectResponse(f"/transcripts/{thread_id}?error={msg}", status_code=303)
