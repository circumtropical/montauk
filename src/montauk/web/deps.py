"""Shared route dependencies: authenticated context, CSRF check, workspace
scope, and the templates object."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..db.crypto import constant_time_equals
from ..db.repositories import Actor, WorkspaceScope
from ..services.auth import AuthContext
from .app import TEMPLATES, db_session

templates = TEMPLATES


def require_auth(request: Request) -> AuthContext:
    ctx: AuthContext | None = getattr(request.state, "auth", None)
    if ctx is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return ctx


def workspace_scope(
    auth: AuthContext = Depends(require_auth),
    session: Session = Depends(db_session),
) -> WorkspaceScope:
    return WorkspaceScope(session, auth.workspace_id, Actor("owner", auth.email))


async def csrf_protect(request: Request, auth: AuthContext = Depends(require_auth)) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    form = await request.form()
    token = str(form.get("_csrf", ""))
    if not token or not constant_time_equals(token, auth.csrf_secret):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")


def page_context(request: Request, auth: AuthContext | None, **extra: object) -> dict[str, object]:
    base: dict[str, object] = {
        "auth": auth,
        "csrf": auth.csrf_secret if auth else "",
        "workspace_slug": auth.workspace_slug if auth else None,
    }
    base.update(extra)
    return base
