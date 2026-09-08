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
from ...services.auth import AuthContext
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
        migrations=migrations,
        **extra,
    )


@router.get("", response_class=HTMLResponse)
def settings_page(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    session: Session = Depends(db_session),
) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "settings.html", _settings_context(request, auth, session))


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
            capabilities=[str(c) for c in caps],
            created_by=auth.user_id,
        )
    except AgentCredentialError as exc:
        return TEMPLATES.TemplateResponse(
            request,
            "settings.html",
            _settings_context(request, auth, session, error=str(exc)),
            status_code=400,
        )
    return TEMPLATES.TemplateResponse(
        request,
        "settings.html",
        _settings_context(
            request,
            auth,
            session,
            flash=f"Created agent credential {cred.name!r}. Copy the token now -- it is not shown again.",
            new_token=raw,
        ),
    )


@router.post("/agents/{credential_id}/revoke", dependencies=[Depends(csrf_protect)])
def revoke_agent(
    credential_id: uuid.UUID,
    auth: AuthContext = Depends(require_auth),
    session: Session = Depends(db_session),
) -> RedirectResponse:
    revoke_credential(session, workspace_id=auth.workspace_id, credential_id=credential_id)
    return RedirectResponse("/settings", status_code=303)
