"""Login / logout (spec 25.1, 27)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ...services.auth import (
    SESSION_COOKIE,
    AccountLocked,
    authenticate,
    create_session,
    destroy_session,
)
from ..app import TEMPLATES, db_session, get_state
from ..deps import csrf_protect

router = APIRouter()


def _client_key(request: Request, email: str) -> str:
    ip = request.client.host if request.client else "?"
    return f"{email.strip().lower()}|{ip}"


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> HTMLResponse:
    if getattr(request.state, "auth", None) is not None:
        return RedirectResponse("/", status_code=303)  # type: ignore[return-value]
    return TEMPLATES.TemplateResponse(request, "login.html", {"auth": None})


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    session: Session = Depends(db_session),
    email: str = Form(...),
    password: str = Form(...),
) -> HTMLResponse:
    state = get_state(request)
    try:
        user = authenticate(
            session,
            email=email,
            password=password,
            throttle=state.throttle,
            throttle_key=_client_key(request, email),
        )
    except AccountLocked:
        return TEMPLATES.TemplateResponse(
            request,
            "login.html",
            {"auth": None, "error": "Too many failed attempts. Try again in a few minutes."},
            status_code=429,
        )
    if user is None:
        return TEMPLATES.TemplateResponse(
            request,
            "login.html",
            {"auth": None, "error": "Incorrect email or password."},
            status_code=401,
        )

    workspace_id = _first_workspace_id(session, user)
    if workspace_id is None:
        return TEMPLATES.TemplateResponse(
            request,
            "login.html",
            {"auth": None, "error": "This account is not a member of any workspace."},
            status_code=403,
        )
    raw = create_session(session, user=user, workspace_id=workspace_id)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        SESSION_COOKIE,
        raw,
        httponly=True,
        samesite="lax",
        secure=state.secure_cookies,
        max_age=12 * 3600,
    )
    return resp  # type: ignore[return-value]


def _first_workspace_id(session: Session, user: object):  # type: ignore[no-untyped-def]
    from ...db import models as orm

    row = (
        session.query(orm.WorkspaceMembership)
        .filter(orm.WorkspaceMembership.user_id == user.id)  # type: ignore[attr-defined]
        .order_by(orm.WorkspaceMembership.created_at)
        .first()
    )
    return row.workspace_id if row else None


@router.post("/logout", dependencies=[Depends(csrf_protect)])
def logout(request: Request, session: Session = Depends(db_session)) -> RedirectResponse:
    destroy_session(session, request.cookies.get(SESSION_COOKIE))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp
