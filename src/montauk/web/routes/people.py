"""People directory + person page (spec 25.3, 25.4)."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy import select

from ...dates import Birthday
from ...db import models as orm
from ...db.mapping import person_to_domain
from ...db.repositories import (
    LocalRecordNotFound,
    PeopleRepository,
    PersonNotFound,
    RelatedPersonInvalid,
    RevisionRepository,
    WorkspaceScope,
)
from ...errors import MontaukValidationError
from ...models import ContactInfo, Person, Source
from ...schema import CATEGORIES, Confidence
from ...services import summaries
from ...services.auth import AuthContext
from ...services.summaries import DETAIL_LEVELS as SUMMARY_DETAIL_LEVELS
from ..app import TEMPLATES, get_state
from ..deps import csrf_protect, page_context, require_auth, workspace_scope

router = APIRouter(prefix="/people")

PAGE_SIZE = 50
CONFIDENCE_LEVELS = [c.value for c in Confidence]


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


def _render_directory(
    request: Request,
    auth: AuthContext,
    scope: WorkspaceScope,
    *,
    q: str = "",
    show: str = "active",
    page: int = 1,
    create_error: str | None = None,
    create_form: dict | None = None,
    status_code: int = 200,
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
            create_error=create_error,
            create_form=create_form or {},
        ),
        status_code=status_code,
    )


@router.get("", response_class=HTMLResponse)
def directory(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    scope: WorkspaceScope = Depends(workspace_scope),
    q: str = Query(""),
    show: str = Query("active"),
    page: int = Query(1, ge=1),
) -> HTMLResponse:
    return _render_directory(request, auth, scope, q=q, show=show, page=page)


@router.post("", response_class=HTMLResponse, dependencies=[Depends(csrf_protect)])
async def create_person(
    request: Request,
    auth: AuthContext = Depends(require_auth),
    scope: WorkspaceScope = Depends(workspace_scope),
) -> Response:
    repo = PeopleRepository(scope)
    f = await request.form()
    values = {
        "name": str(f.get("name", "")),
        "summary": str(f.get("summary", "")),
        "company": str(f.get("company", "")),
        "location": str(f.get("location", "")),
    }
    try:
        person = Person(
            id=repo.allocate_public_id(),
            name=values["name"],
            summary=_clean(values["summary"]),
            company=_clean(values["company"]),
            location=_clean(values["location"]),
        )
    except ValidationError as exc:
        scope.session.rollback()
        return _render_directory(
            request, auth, scope, create_error=_err_message(exc), create_form=values, status_code=400
        )
    repo.create(person)
    scope.session.flush()
    dupes = repo.find_by_name(person.name, exclude_public_id=person.id)
    dest = f"/people/{person.id}"
    if dupes:
        dest += "?dupes=" + ",".join(d.public_id for d in dupes)
    return RedirectResponse(dest, status_code=303)


def _load(scope: WorkspaceScope, public_id: str) -> tuple[PeopleRepository, orm.Person]:
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
    row: orm.Person,
    *,
    error: str | None = None,
    form: dict | None = None,
    status_code: int = 200,
    summary_result: object | None = None,
) -> HTMLResponse:
    domain = person_to_domain(row)
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
    revisions = RevisionRepository(scope).for_person(row.id, limit=100)

    cached_summaries = list(
        scope.session.execute(
            select(orm.SummaryCacheEntry)
            .where(orm.SummaryCacheEntry.workspace_id == scope.workspace_id)
            .where(orm.SummaryCacheEntry.person_id == row.id)
            .where(orm.SummaryCacheEntry.invalidated_at.is_(None))
            .order_by(orm.SummaryCacheEntry.created_at.desc())
        ).scalars()
    )

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
            confidence_levels=CONFIDENCE_LEVELS,
            facts_by_category=facts_by_category,
            relationships=relationships,
            rel_display=rel_display,
            interactions=interactions,
            contact=domain.contact,
            revisions=revisions,
            edit_error=error,
            form=form or _person_form_values(domain),
            flash=_dupes_flash(request),
            summary_result=summary_result,
            cached_summaries=cached_summaries,
            summary_detail_levels=SUMMARY_DETAIL_LEVELS,
        ),
        status_code=status_code,
    )


def _dupes_flash(request: Request) -> str | None:
    raw = request.query_params.get("dupes")
    if not raw:
        return None
    ids = [p for p in raw.split(",") if p]
    return f"Created. Note: {len(ids)} other record(s) share this name ({', '.join(ids)})."


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


# --- inline fact / interaction editing (spec 25.4) ------------------

_OP_ERRORS = (ValidationError, RelatedPersonInvalid, LocalRecordNotFound, MontaukValidationError, ValueError)


def _sources_from_form(raw: str) -> list[Source]:
    out: list[Source] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or (":" not in line and "=" not in line):
            continue
        sep = ":" if ":" in line else "="
        stype, _, sref = line.partition(sep)
        if stype.strip() and sref.strip():
            out.append(Source(type=stype.strip(), id=sref.strip()))
    return out


def _err_message(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        e = exc.errors()[0]
        loc = ".".join(str(p) for p in e["loc"]) or "field"
        return f"{loc}: {e['msg']}"
    return str(exc)


async def _run_op(
    request: Request,
    auth: AuthContext,
    scope: WorkspaceScope,
    public_id: str,
    op: Callable[[PeopleRepository, orm.Person], object],
) -> Response:
    repo, row = _load(scope, public_id)
    try:
        op(repo, row)
    except _OP_ERRORS as exc:
        return _render_person(request, auth, scope, repo, row, error=_err_message(exc), status_code=400)
    return RedirectResponse(f"/people/{public_id}", status_code=303)


@router.post("/{public_id}/facts", response_class=HTMLResponse, dependencies=[Depends(csrf_protect)])
async def add_fact(
    request: Request,
    public_id: str,
    auth: AuthContext = Depends(require_auth),
    scope: WorkspaceScope = Depends(workspace_scope),
) -> Response:
    f = await request.form()
    return await _run_op(
        request,
        auth,
        scope,
        public_id,
        lambda repo, row: repo.add_fact(
            row,
            category=str(f.get("category", "")),
            text=str(f.get("text", "")),
            date=_clean(str(f.get("date", ""))),
            confidence=str(f.get("confidence", "high")) or "high",
            related_person_id=_clean(str(f.get("related_person_id", ""))),
            sources=_sources_from_form(str(f.get("sources", ""))),
            reason=_clean(str(f.get("_reason", ""))),
        ),
    )


@router.post(
    "/{public_id}/facts/{local_id}", response_class=HTMLResponse, dependencies=[Depends(csrf_protect)]
)
async def update_fact(
    request: Request,
    public_id: str,
    local_id: str,
    auth: AuthContext = Depends(require_auth),
    scope: WorkspaceScope = Depends(workspace_scope),
) -> Response:
    f = await request.form()
    related = _clean(str(f.get("related_person_id", "")))
    return await _run_op(
        request,
        auth,
        scope,
        public_id,
        lambda repo, row: repo.update_fact(
            row,
            local_id,
            text=str(f.get("text", "")),
            category=str(f.get("category", "")),
            date=str(f.get("date", "")),
            confidence=str(f.get("confidence", "")) or None,
            related_person_id=related,
            clear_related=(related is None),
            sources=_sources_from_form(str(f.get("sources", ""))),
            reason=_clean(str(f.get("_reason", ""))),
        ),
    )


@router.post(
    "/{public_id}/facts/{local_id}/delete",
    dependencies=[Depends(csrf_protect)],
)
async def delete_fact(
    request: Request,
    public_id: str,
    local_id: str,
    auth: AuthContext = Depends(require_auth),
    scope: WorkspaceScope = Depends(workspace_scope),
) -> Response:
    f = await request.form()
    return await _run_op(
        request,
        auth,
        scope,
        public_id,
        lambda repo, row: repo.remove_fact(row, local_id, reason=_clean(str(f.get("_reason", "")))),
    )


@router.post("/{public_id}/interactions", response_class=HTMLResponse, dependencies=[Depends(csrf_protect)])
async def add_interaction(
    request: Request,
    public_id: str,
    auth: AuthContext = Depends(require_auth),
    scope: WorkspaceScope = Depends(workspace_scope),
) -> Response:
    f = await request.form()
    return await _run_op(
        request,
        auth,
        scope,
        public_id,
        lambda repo, row: repo.add_interaction(
            row,
            date=str(f.get("date", "")),
            channel=_clean(str(f.get("channel", ""))),
            connection_level=_int_or_none(str(f.get("connection_level", ""))),
            summary=_clean(str(f.get("summary", ""))),
            sources=_sources_from_form(str(f.get("sources", ""))),
            reason=_clean(str(f.get("_reason", ""))),
        ),
    )


@router.post(
    "/{public_id}/interactions/{local_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(csrf_protect)],
)
async def update_interaction(
    request: Request,
    public_id: str,
    local_id: str,
    auth: AuthContext = Depends(require_auth),
    scope: WorkspaceScope = Depends(workspace_scope),
) -> Response:
    f = await request.form()
    level_raw = str(f.get("connection_level", "")).strip()
    return await _run_op(
        request,
        auth,
        scope,
        public_id,
        lambda repo, row: repo.update_interaction(
            row,
            local_id,
            date=_clean(str(f.get("date", ""))),
            channel=str(f.get("channel", "")),
            connection_level=_int_or_none(level_raw),
            clear_connection_level=(level_raw == ""),
            summary=str(f.get("summary", "")),
            sources=_sources_from_form(str(f.get("sources", ""))),
            reason=_clean(str(f.get("_reason", ""))),
        ),
    )


@router.post(
    "/{public_id}/interactions/{local_id}/delete",
    dependencies=[Depends(csrf_protect)],
)
async def delete_interaction(
    request: Request,
    public_id: str,
    local_id: str,
    auth: AuthContext = Depends(require_auth),
    scope: WorkspaceScope = Depends(workspace_scope),
) -> Response:
    f = await request.form()
    return await _run_op(
        request,
        auth,
        scope,
        public_id,
        lambda repo, row: repo.remove_interaction(row, local_id, reason=_clean(str(f.get("_reason", "")))),
    )


def _int_or_none(raw: str) -> int | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    return int(raw)  # ValueError -> handled by _run_op


# --- purpose-specific summary (spec 24.2) ---------------------------


@router.post("/{public_id}/summary", response_class=HTMLResponse, dependencies=[Depends(csrf_protect)])
async def generate_person_summary(
    request: Request,
    public_id: str,
    auth: AuthContext = Depends(require_auth),
    scope: WorkspaceScope = Depends(workspace_scope),
) -> Response:
    repo, row = _load(scope, public_id)
    f = await request.form()
    purpose = str(f.get("purpose", "")).strip()
    detail_level = str(f.get("detail_level", "standard"))
    force = f.get("force") is not None
    if not purpose:
        return _render_person(
            request,
            auth,
            scope,
            repo,
            row,
            error="Enter a purpose for the summary (the question or task it should serve).",
            status_code=400,
        )
    if detail_level not in SUMMARY_DETAIL_LEVELS:
        detail_level = "standard"
    try:
        result = await summaries.generate_summary(
            scope.session,
            scope,
            public_id=public_id,
            purpose=purpose,
            detail_level=detail_level,
            secret_box=get_state(request).secret_box,
            force=force,
        )
    except ValueError as exc:
        return _render_person(request, auth, scope, repo, row, error=str(exc), status_code=400)
    return _render_person(request, auth, scope, repo, row, summary_result=result)
