"""First-run setup wizard (spec 25.1).

This build covers steps 1-3: owner credentials, workspace name, and the
public/base URL + deployment profile. LLM, connectors, backups, and agent
connection (steps 4-8) are handled once their subsystems land; the wizard
links to Settings for what is available now.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ...services.auth import SESSION_COOKIE, create_session
from ...services.workspace import DeploymentAlreadyInitialized, bootstrap_deployment, is_initialized
from ..app import TEMPLATES, db_session, get_state

router = APIRouter()

MIN_PASSWORD_LEN = 10


@router.get("/setup", response_class=HTMLResponse)
def setup_form(request: Request, session: Session = Depends(db_session)) -> HTMLResponse:
    if is_initialized(session):
        return RedirectResponse("/", status_code=303)  # type: ignore[return-value]
    return TEMPLATES.TemplateResponse(request, "setup.html", {"auth": None})


@router.post("/setup", response_class=HTMLResponse)
def setup_submit(
    request: Request,
    session: Session = Depends(db_session),
    email: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    workspace_name: str = Form(...),
    public_url: str = Form(""),
    deployment_profile: str = Form("private"),
) -> HTMLResponse:
    def fail(msg: str) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "setup.html",
            {
                "auth": None,
                "error": msg,
                "values": {
                    "email": email,
                    "workspace_name": workspace_name,
                    "public_url": public_url,
                    "deployment_profile": deployment_profile,
                },
            },
            status_code=400,
        )

    if is_initialized(session):
        return RedirectResponse("/", status_code=303)  # type: ignore[return-value]
    if "@" not in email or len(email) < 3:
        return fail("Enter a valid email address.")
    if len(password) < MIN_PASSWORD_LEN:
        return fail(f"Password must be at least {MIN_PASSWORD_LEN} characters.")
    if password != password_confirm:
        return fail("Passwords do not match.")
    if not workspace_name.strip():
        return fail("Give the workspace a name.")
    if deployment_profile not in ("private", "public"):
        return fail("Choose a deployment profile.")
    if deployment_profile == "public" and not public_url.startswith("https://"):
        return fail("A public deployment needs an https:// base URL.")

    try:
        user, workspace = bootstrap_deployment(
            session,
            email=email,
            password=password,
            workspace_name=workspace_name,
            public_url=public_url or None,
            deployment_profile=deployment_profile,
        )
    except DeploymentAlreadyInitialized:
        return RedirectResponse("/", status_code=303)  # type: ignore[return-value]

    session.flush()
    raw = create_session(session, user=user, workspace_id=workspace.id)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        SESSION_COOKIE,
        raw,
        httponly=True,
        samesite="lax",
        secure=get_state(request).secure_cookies,
        max_age=12 * 3600,
    )
    return resp  # type: ignore[return-value]
