"""Dashboard home page (spec 25.2)."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import models as orm
from ...db.repositories import PeopleRepository, WorkspaceScope, overdue_contacts, upcoming_birthdays
from ...services.auth import AuthContext
from ..app import TEMPLATES, db_session
from ..deps import page_context, require_auth, workspace_scope

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    session: Session = Depends(db_session),
    scope: WorkspaceScope = Depends(workspace_scope),
) -> HTMLResponse:
    repo = PeopleRepository(scope)
    today = dt.date.today()

    last_migration = session.execute(
        select(orm.LegacyMigrationRun)
        .where(orm.LegacyMigrationRun.workspace_id == auth.workspace_id)
        .order_by(orm.LegacyMigrationRun.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    ctx = page_context(
        request,
        auth,
        people_count=repo.count(archived=False),
        archived_count=repo.count(archived=True),
        birthdays=[
            {"person": p, "on": occ.isoformat(), "days": days}
            for p, occ, days in upcoming_birthdays(scope, within_days=30, today=today)
        ],
        overdue=overdue_contacts(scope, today=today),
        last_migration=last_migration,
        # Subsystems not in this build -- shown as explicit "not configured"
        # cards rather than hidden (spec 25.2, 25 degraded states).
        deferred={
            "pending_reviews": "extraction not configured",
            "unprocessed_messages": "no connectors configured",
            "connector_health": "no connectors configured",
            "llm_status": "not configured",
            "last_extraction": "extraction not configured",
            "last_backup": "not configured",
        },
    )
    return TEMPLATES.TemplateResponse(request, "home.html", ctx)


@router.get("/transcripts", response_class=HTMLResponse)
def transcripts(request: Request, auth: AuthContext = Depends(require_auth)) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request,
        "transcripts.html",
        page_context(request, auth),
    )
