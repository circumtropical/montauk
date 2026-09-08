"""People directory + person page (spec 25.3, 25.4)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ...db.mapping import person_to_domain
from ...db.repositories import (
    PeopleRepository,
    PersonNotFound,
    RevisionRepository,
    WorkspaceScope,
)
from ...schema import CATEGORIES
from ...services.auth import AuthContext
from ..app import TEMPLATES
from ..deps import csrf_protect, page_context, require_auth, workspace_scope

router = APIRouter(prefix="/people")

PAGE_SIZE = 50


@router.get("", response_class=HTMLResponse)
def directory(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    scope: WorkspaceScope = Depends(workspace_scope),
    q: str = Query(""),
    show: str = Query("active"),
    page: int = Query(1, ge=1),
) -> HTMLResponse:
    repo = PeopleRepository(scope)
    archived = {"active": False, "archived": True, "all": None}.get(show, False)
    offset = (page - 1) * PAGE_SIZE
    rows = repo.list_people(query=q or None, archived=archived, limit=PAGE_SIZE + 1, offset=offset)
    has_next = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]

    name_counts: dict[str, int] = {}
    for r in rows:
        name_counts[r.name.strip().lower()] = name_counts.get(r.name.strip().lower(), 0) + 1

    people = [
        {
            "public_id": r.public_id,
            "name": r.name,
            "company": r.company,
            "location": r.location,
            "summary": r.summary,
            "archived": r.archived_at is not None,
            "aliases": [a.alias for a in sorted(r.aliases, key=lambda a: a.position)],
            "shared_name": name_counts.get(r.name.strip().lower(), 0) > 1,
        }
        for r in rows
    ]
    return TEMPLATES.TemplateResponse(
        request,
        "people_list.html",
        page_context(
            request,
            auth,
            people=people,
            q=q,
            show=show,
            page=page,
            has_next=has_next,
            active_count=repo.count(archived=False),
            archived_count=repo.count(archived=True),
        ),
    )


def _load(scope: WorkspaceScope, public_id: str):  # type: ignore[no-untyped-def]
    repo = PeopleRepository(scope)
    try:
        return repo, repo.require(public_id, include_archived=True)
    except PersonNotFound:
        raise HTTPException(status_code=404, detail="No such person in this workspace") from None


@router.get("/{public_id}", response_class=HTMLResponse)
def person_page(
    request: Request,
    public_id: str,
    auth: AuthContext = Depends(require_auth),
    scope: WorkspaceScope = Depends(workspace_scope),
) -> HTMLResponse:
    repo, row = _load(scope, public_id)
    domain = person_to_domain(row)

    facts_by_category = {
        c: [f for f in domain.facts if f.category == c and not f.related_person_id] for c in CATEGORIES
    }
    relationships = [f for f in domain.facts if f.related_person_id]
    rel_names = repo.resolve_public_ids({f.related_person_id for f in relationships if f.related_person_id})
    rel_display = {}
    for pid in rel_names:
        other = repo.get(pid, include_archived=True)
        if other is not None:
            rel_display[pid] = other.name

    interactions = sorted(domain.interactions, key=lambda i: i.date.latest(), reverse=True)
    revisions = RevisionRepository(scope).for_person(row.id, limit=100)

    return TEMPLATES.TemplateResponse(
        request,
        "person.html",
        page_context(
            request,
            auth,
            p=domain,
            row=row,
            archived=row.archived_at is not None,
            categories=CATEGORIES,
            facts_by_category=facts_by_category,
            relationships=relationships,
            rel_display=rel_display,
            interactions=interactions,
            contact=domain.contact,
            revisions=revisions,
        ),
    )


@router.post("/{public_id}/archive", dependencies=[Depends(csrf_protect)])
def archive(
    public_id: str,
    scope: WorkspaceScope = Depends(workspace_scope),
    reason: str = Form(""),
) -> RedirectResponse:
    repo, row = _load(scope, public_id)
    repo.set_archived(row, True, reason=reason or None)
    return RedirectResponse(f"/people/{public_id}", status_code=303)


@router.post("/{public_id}/restore", dependencies=[Depends(csrf_protect)])
def restore(
    public_id: str,
    scope: WorkspaceScope = Depends(workspace_scope),
) -> RedirectResponse:
    repo, row = _load(scope, public_id)
    repo.set_archived(row, False)
    return RedirectResponse(f"/people/{public_id}", status_code=303)
