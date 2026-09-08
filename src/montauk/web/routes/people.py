"""People directory + person page (spec 25.3, 25.4)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from ...dates import Birthday
from ...db.mapping import person_to_domain
from ...db.repositories import (
    PeopleRepository,
    PersonNotFound,
    RevisionRepository,
    WorkspaceScope,
)
from ...errors import MontaukValidationError
from ...models import ContactInfo, Person
from ...schema import CATEGORIES
from ...services.auth import AuthContext
from ..app import TEMPLATES
from ..deps import csrf_protect, page_context, require_auth, workspace_scope

router = APIRouter(prefix="/people")

PAGE_SIZE = 50


def _lines(raw: str) -> list[str]:
    return [ln.strip() for ln in (raw or "").replace(",", "\n").splitlines() if ln.strip()]


def _clean(raw: str) -> str | None:
    v = (raw or "").strip()
    return v or None


def _parse_messaging(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        sep = ":" if ":" in line else ("=" if "=" in line else None)
        if sep is None:
            continue
        key, _, val = line.partition(sep)
        if key.strip() and val.strip():
            out[key.strip()] = val.strip()
    return out


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


def _render_person(
    request: Request,
    auth: AuthContext,
    scope: WorkspaceScope,
    repo: PeopleRepository,
    row: object,
    *,
    error: str | None = None,
    form: dict | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    domain = person_to_domain(row)  # type: ignore[arg-type]
    facts_by_category = {
        c: [f for f in domain.facts if f.category == c and not f.related_person_id] for c in CATEGORIES
    }
    relationships = [f for f in domain.facts if f.related_person_id]
    rel_display = {}
    for pid in repo.resolve_public_ids({f.related_person_id for f in relationships if f.related_person_id}):
        other = repo.get(pid, include_archived=True)
        if other is not None:
            rel_display[pid] = other.name

    interactions = sorted(domain.interactions, key=lambda i: i.date.latest(), reverse=True)
    revisions = RevisionRepository(scope).for_person(row.id, limit=100)  # type: ignore[attr-defined]

    return TEMPLATES.TemplateResponse(
        request,
        "person.html",
        page_context(
            request,
            auth,
            p=domain,
            row=row,
            archived=row.archived_at is not None,  # type: ignore[attr-defined]
            categories=CATEGORIES,
            facts_by_category=facts_by_category,
            relationships=relationships,
            rel_display=rel_display,
            interactions=interactions,
            contact=domain.contact,
            revisions=revisions,
            edit_error=error,
            form=form or _person_form_values(domain),
        ),
        status_code=status_code,
    )


def _person_form_values(domain: Person) -> dict:
    c = domain.contact
    return {
        "name": domain.name,
        "aliases": "\n".join(domain.aliases),
        "birthday": domain.birthday.to_string() if domain.birthday else "",
        "location": domain.location or "",
        "company": domain.company or "",
        "job_title": domain.job_title or "",
        "desired_contact_cadence_days": (
            str(domain.desired_contact_cadence_days)
            if domain.desired_contact_cadence_days is not None
            else ""
        ),
        "summary": domain.summary or "",
        "emails": "\n".join(c.emails),
        "phones": "\n".join(c.phones),
        "address": c.address or "",
        "messaging": "\n".join(f"{k}: {v}" for k, v in c.messaging.items()),
    }


@router.get("/{public_id}", response_class=HTMLResponse)
def person_page(
    request: Request,
    public_id: str,
    auth: AuthContext = Depends(require_auth),
    scope: WorkspaceScope = Depends(workspace_scope),
) -> HTMLResponse:
    repo, row = _load(scope, public_id)
    return _render_person(request, auth, scope, repo, row)


@router.post("/{public_id}/edit", response_class=HTMLResponse, dependencies=[Depends(csrf_protect)])
async def edit_person(
    request: Request,
    public_id: str,
    auth: AuthContext = Depends(require_auth),
    scope: WorkspaceScope = Depends(workspace_scope),
) -> HTMLResponse:
    repo, row = _load(scope, public_id)
    f = await request.form()
    values = {k: str(f.get(k, "")) for k in _person_form_values(person_to_domain(row))}
    cadence_raw = _clean(values["desired_contact_cadence_days"])
    try:
        cadence = int(cadence_raw) if cadence_raw is not None else None
    except ValueError:
        return _render_person(
            request,
            auth,
            scope,
            repo,
            row,
            error="Contact cadence must be a whole number of days (or blank).",
            form=values,
            status_code=400,
        )
    try:
        bday_raw = _clean(values["birthday"])
        birthday = Birthday.parse(bday_raw) if bday_raw is not None else None
        updated = Person(
            id=row.public_id,
            name=values["name"],
            aliases=_lines(values["aliases"]),
            birthday=birthday,
            location=_clean(values["location"]),
            company=_clean(values["company"]),
            job_title=_clean(values["job_title"]),
            desired_contact_cadence_days=cadence,
            summary=_clean(values["summary"]),
            contact=ContactInfo(
                emails=_lines(values["emails"]),
                phones=_lines(values["phones"]),
                address=_clean(values["address"]),
                messaging=_parse_messaging(values["messaging"]),
            ),
        )
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first["loc"]) or "field"
        return _render_person(
            request,
            auth,
            scope,
            repo,
            row,
            error=f"{loc}: {first['msg']}",
            form=values,
            status_code=400,
        )
    except ValueError as exc:
        return _render_person(
            request,
            auth,
            scope,
            repo,
            row,
            error=f"birthday: {exc}",
            form=values,
            status_code=400,
        )

    try:
        repo.update_core_fields(row, updated, reason=_clean(str(f.get("_reason", ""))))
    except MontaukValidationError as exc:
        return _render_person(request, auth, scope, repo, row, error=str(exc), form=values, status_code=400)
    return RedirectResponse(f"/people/{public_id}", status_code=303)  # type: ignore[return-value]


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
