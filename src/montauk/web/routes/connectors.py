"""Inbound connectors (spec 10, 11, 25.6).

Montauk **never sends** on a connector -- these routes only pair, discover,
select threads, sync inbound messages, and disconnect.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from ...connectors import service
from ...connectors.base import ConnectorError
from ...connectors.sidecar import sidecar_configured
from ...db.repositories import WorkspaceScope
from ...services.auth import AuthContext
from ..app import TEMPLATES, db_session, get_state
from ..deps import csrf_protect, page_context, require_auth, workspace_scope

router = APIRouter(prefix="/connectors")

_HISTORY_WINDOWS = ("all", "since_date", "future_only")


def _ctx(request: Request, auth: AuthContext, session: Session, **extra: object) -> dict:
    return page_context(
        request,
        auth,
        sidecar_configured=sidecar_configured(),
        master_key_present=get_state(request).secret_box is not None,
        account=service.account_view(session, auth.workspace_id),
        **extra,
    )


@router.get("", response_class=HTMLResponse)
def index(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    session: Session = Depends(db_session),
    error: str | None = None,
    notice: str | None = None,
) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request, "connectors.html", _ctx(request, auth, session, error=error, notice=notice)
    )


def _redirect(msg: str | None = None, *, error: bool = False) -> RedirectResponse:
    q = f"?{'error' if error else 'notice'}={msg}" if msg else ""
    return RedirectResponse(f"/connectors{q}", status_code=303)


def _guard(request: Request) -> str | None:
    if not sidecar_configured():
        return "The WhatsApp sidecar is not configured on this deployment."
    if get_state(request).secret_box is None:
        return "Set MONTAUK_MASTER_KEY before connecting -- the linked-device session must be encrypted."
    return None


@router.post("/whatsapp/connect", dependencies=[Depends(csrf_protect)])
async def connect(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    session: Session = Depends(db_session),
    scope: WorkspaceScope = Depends(workspace_scope),
) -> Response:
    if msg := _guard(request):
        return _redirect(msg, error=True)
    connector = service.build_connector()
    try:
        await service.begin_pairing(session, scope, connector, created_by=auth.user_id)
    except ConnectorError as exc:
        return _redirect(str(exc), error=True)
    return RedirectResponse("/connectors/whatsapp/pair", status_code=303)


@router.get("/whatsapp/pair", response_class=HTMLResponse)
def pair_page(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    session: Session = Depends(db_session),
) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(request, "connector_pair.html", _ctx(request, auth, session))


@router.get("/whatsapp/pair.json")
async def pair_status(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    session: Session = Depends(db_session),
    scope: WorkspaceScope = Depends(workspace_scope),
) -> JSONResponse:
    account = service.get_account(session, auth.workspace_id)
    if account is None:
        return JSONResponse({"state": "unconfigured"}, headers={"Cache-Control": "no-store"})
    connector = service.build_connector()
    box = get_state(request).secret_box
    try:
        st = await service.refresh_status(session, scope, account, connector, box)
    except ConnectorError as exc:
        return JSONResponse(
            {"state": account.status, "error": str(exc)}, headers={"Cache-Control": "no-store"}
        )
    if st.state == "connected":
        try:
            await service.discover(session, scope, account, connector)
        except ConnectorError:
            pass
    return JSONResponse(
        {
            "state": st.state,
            "qr": st.pairing_code,
            "connected_as": account.self_phone,
            "error": st.last_error,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/whatsapp/discover", dependencies=[Depends(csrf_protect)])
async def rediscover(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    session: Session = Depends(db_session),
    scope: WorkspaceScope = Depends(workspace_scope),
) -> Response:
    account = service.get_account(session, auth.workspace_id)
    if account is None:
        return _redirect("Connect WhatsApp first.", error=True)
    try:
        n = await service.discover(session, scope, account, service.build_connector())
    except ConnectorError as exc:
        return _redirect(str(exc), error=True)
    return _redirect(f"Found {n} conversation(s).")


@router.post("/whatsapp/sync", dependencies=[Depends(csrf_protect)])
async def sync_now(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    session: Session = Depends(db_session),
    scope: WorkspaceScope = Depends(workspace_scope),
) -> Response:
    account = service.get_account(session, auth.workspace_id)
    if account is None:
        return _redirect("Connect WhatsApp first.", error=True)
    try:
        counts = await service.sync_account(
            session, scope, account, service.build_connector(), get_state(request).secret_box
        )
    except ConnectorError as exc:
        return _redirect(str(exc), error=True)
    return _redirect(f"Synced {counts['live']} new + {counts['history']} historical message(s).")


@router.post("/whatsapp/threads/{thread_id}", dependencies=[Depends(csrf_protect)])
async def toggle_thread(
    thread_id: uuid.UUID,
    request: Request,
    auth: AuthContext = Depends(require_auth),
    session: Session = Depends(db_session),
    scope: WorkspaceScope = Depends(workspace_scope),
) -> Response:
    account = service.get_account(session, auth.workspace_id)
    if account is None:
        return _redirect("Connect WhatsApp first.", error=True)
    form = await request.form()
    enabled = str(form.get("enabled", "")) == "1"
    window = str(form.get("history_window", "all"))
    if window not in _HISTORY_WINDOWS:
        window = "all"
    try:
        service.set_thread_enabled(session, scope, account, thread_id, enabled=enabled, history_window=window)
    except ConnectorError as exc:
        return _redirect(str(exc), error=True)
    return _redirect(
        "Thread enabled -- history will sync on the next pass." if enabled else "Thread disabled."
    )


@router.post("/whatsapp/disconnect", dependencies=[Depends(csrf_protect)])
async def disconnect(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    session: Session = Depends(db_session),
    scope: WorkspaceScope = Depends(workspace_scope),
) -> Response:
    account = service.get_account(session, auth.workspace_id)
    if account is None:
        return _redirect()
    form = await request.form()
    logout = str(form.get("logout", "")) == "1"
    await service.disconnect(session, scope, account, service.build_connector(), logout=logout)
    return _redirect("Device unlinked. Archived transcripts are kept." if logout else "Disconnected.")
