"""Transcripts: import a WhatsApp .txt export, map its participants to
people, and run the extraction model over the new messages (spec 14-17)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile as StarletteUploadFile

from ...db.repositories import PeopleRepository, WorkspaceScope
from ...services import extraction, model_config, transcripts
from ...services.auth import AuthContext
from ...services.transcripts import TranscriptError
from ..app import TEMPLATES, db_session, get_state
from ..deps import csrf_protect, page_context, require_auth, workspace_scope

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
    error: str | None = None,
    result: str | None = None,
) -> HTMLResponse:
    try:
        view = transcripts.get_thread(session, scope, thread_id)
    except TranscriptError:
        return RedirectResponse("/transcripts", status_code=303)  # type: ignore[return-value]
    people = PeopleRepository(scope).list_people(archived=False, limit=500)
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
            extraction_result=result,
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
    res = await extraction.run_extraction(
        session, scope, thread_id=thread_id, secret_box=get_state(request).secret_box
    )
    summary = (
        f"{res.status}: {res.facts_added} fact(s), {res.interactions_touched} interaction(s) "
        f"across {res.days_processed} day(s); {res.awaiting_remaining} pending"
        + (f" -- {res.note}" if res.note else "")
    )
    return RedirectResponse(f"/transcripts/{thread_id}?result={summary}", status_code=303)


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
