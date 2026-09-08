"""Settings & administration (spec 25.6)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import models as orm
from ...services.agent_credentials import (
    CAPABILITIES,
    DEFAULT_CAPABILITIES,
    AgentCredentialError,
    create_credential,
    list_credentials,
    revoke_credential,
)
from ...services.auth import AuthContext, PasswordChangeError, change_password
from ...services.settings import (
    ALLOWED_REVIEWERS,
    DEPLOYMENT_PROFILES,
    HISTORICAL_INGESTION,
    REVIEW_THRESHOLDS,
    SettingsError,
    update_review_policy,
    update_workspace,
)
from ..app import TEMPLATES, db_session
from ..deps import csrf_protect, page_context, require_auth

router = APIRouter(prefix="/settings")


def _settings_context(request: Request, auth: AuthContext, session: Session, **extra: object) -> dict:
    workspace = session.get(orm.Workspace, auth.workspace_id)
    ws_settings = session.get(orm.WorkspaceSettings, auth.workspace_id)
    migrations = list(
        session.execute(
            select(orm.LegacyMigrationRun)
            .where(orm.LegacyMigrationRun.workspace_id == auth.workspace_id)
            .order_by(orm.LegacyMigrationRun.started_at.desc())
        ).scalars()
    )
    return page_context(
        request,
        auth,
        workspace=workspace,
        ws_settings=ws_settings,
        credentials=list_credentials(session, auth.workspace_id),
        capabilities=CAPABILITIES,
        default_capabilities=DEFAULT_CAPABILITIES,
        review_thresholds=REVIEW_THRESHOLDS,
        allowed_reviewers_options=ALLOWED_REVIEWERS,
        deployment_profiles=DEPLOYMENT_PROFILES,
        historical_ingestion_options=HISTORICAL_INGESTION,
        migrations=migrations,
        **extra,
    )


def _page(
    request: Request,
    auth: AuthContext,
    session: Session,
    *,
    status_code: int = 200,
    **extra: object,
) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request,
        "settings.html",
        _settings_context(request, auth, session, **extra),
        status_code=status_code,
    )


@router.get("", response_class=HTMLResponse)
def settings_page(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    session: Session = Depends(db_session),
) -> HTMLResponse:
    return _page(request, auth, session)


@router.post("/workspace", response_class=HTMLResponse, dependencies=[Depends(csrf_protect)])
async def save_workspace(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    session: Session = Depends(db_session),
) -> HTMLResponse:
    f = await request.form()
    try:
        update_workspace(
            session,
            auth.workspace_id,
            name=str(f.get("name", "")),
            deployment_profile=str(f.get("deployment_profile", "private")),
            public_url=str(f.get("public_url", "")),
        )
    except SettingsError as exc:
        return _page(request, auth, session, error=str(exc), status_code=400)
    return _page(request, auth, session, flash="Workspace settings saved.")


@router.post("/review-policy", response_class=HTMLResponse, dependencies=[Depends(csrf_protect)])
async def save_review_policy(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    session: Session = Depends(db_session),
) -> HTMLResponse:
    f = await request.form()
    try:
        update_review_policy(
            session,
            auth.workspace_id,
            review_threshold=str(f.get("review_threshold", "")),
            allowed_reviewers=str(f.get("allowed_reviewers", "")),
            timezone=str(f.get("timezone", "UTC")),
            daily_extraction_time=str(f.get("daily_extraction_time", "")),
            historical_ingestion_default=str(f.get("historical_ingestion_default", "")),
            agent_transcript_access=f.get("agent_transcript_access") is not None,
        )
    except SettingsError as exc:
        return _page(request, auth, session, error=str(exc), status_code=400)
    return _page(request, auth, session, flash="Processing & review settings saved.")


@router.post("/password", response_class=HTMLResponse, dependencies=[Depends(csrf_protect)])
async def change_password_route(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    session: Session = Depends(db_session),
) -> HTMLResponse:
    f = await request.form()
    try:
        change_password(
            session,
            user_id=auth.user_id,
            current_password=str(f.get("current_password", "")),
            new_password=str(f.get("new_password", "")),
            new_password_confirm=str(f.get("new_password_confirm", "")),
            keep_session_token_hash=auth.session_token_hash,
        )
    except PasswordChangeError as exc:
        return _page(request, auth, session, error=str(exc), status_code=400)
    return _page(request, auth, session, flash="Password changed. Other sessions have been signed out.")


@router.post("/agents", response_class=HTMLResponse, dependencies=[Depends(csrf_protect)])
async def create_agent(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    session: Session = Depends(db_session),
) -> HTMLResponse:
    form = await request.form()
    name = str(form.get("name", ""))
    caps = [str(c) for c in form.getlist("capabilities")]
    try:
        cred, raw = create_credential(
            session,
            workspace_id=auth.workspace_id,
            name=name,
            capabilities=caps,
            created_by=auth.user_id,
        )
    except AgentCredentialError as exc:
        return _page(request, auth, session, error=str(exc), status_code=400)
    return _page(
        request,
        auth,
        session,
        flash=f"Created agent credential {cred.name!r}. Copy the token now -- it is not shown again.",
        new_token=raw,
    )


@router.post("/agents/{credential_id}/revoke", dependencies=[Depends(csrf_protect)])
def revoke_agent(
    credential_id: uuid.UUID,
    auth: AuthContext = Depends(require_auth),
    session: Session = Depends(db_session),
) -> RedirectResponse:
    revoke_credential(session, workspace_id=auth.workspace_id, credential_id=credential_id)
    return RedirectResponse("/settings", status_code=303)
