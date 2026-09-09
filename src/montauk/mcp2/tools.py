"""Phase 2 MCP tools, backed by the PostgreSQL store.

Reads lead with prepare_person_briefing (a Montauk-LLM report + source
refs); the raw record is reachable via get_context_sources / get_facts /
get_interactions / get_full_record for verification and detail. Writes go
through the workspace-scoped repository, which records field-level
revisions and resolves relationship references.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.orm import Session

from ..dates import Birthday
from ..db import mapping
from ..db import models as orm
from ..db.repositories import (
    Actor,
    LocalRecordNotFound,
    PeopleRepository,
    PersonNotFound,
    RelatedPersonInvalid,
    WorkspaceScope,
)
from ..errors import MontaukValidationError, NotFoundError, PermissionDeniedError
from ..exporters.markdown import person_to_markdown
from ..ids import normalize_alias
from ..models import Fact, Interaction, Person
from ..person_context import DETAIL_LEVELS, build_person_context
from ..schema import CATEGORIES
from ..services import briefing
from ..services.agent_credentials import authenticate, has_capability
from ..tool_types import (
    AddFactOp,
    BatchOperation,
    OverdueContact,
    PersonCandidate,
    PersonContextResponse,
    PersonCore,
    RecordInteractionOp,
    RemoveFactOp,
    SearchResult,
    SetBirthdayOp,
    SetContactCadenceOp,
    SetNameOp,
    UpcomingBirthday,
    UpdateContactDetailsOp,
    UpdateFactOp,
    UpdateSummaryOp,
    WriteResult,
)
from .auth import extract_bearer_token
from .context import Mcp2Context

READ = "memory_read"
WRITE = "memory_write"


def _person_core(person: Person) -> PersonCore:
    return PersonCore(
        id=person.id,
        name=person.name,
        aliases=person.aliases,
        birthday=person.birthday.to_string() if person.birthday else None,
        location=person.location,
        company=person.company,
        job_title=person.job_title,
        desired_contact_cadence_days=person.desired_contact_cadence_days,
        summary=person.summary,
        contact=person.contact,
    )


def _apply_name_update(
    person: Person,
    *,
    name: str,
    retain_previous_as_alias: bool,
    aliases_to_add: list[str],
    aliases_to_remove: list[str],
) -> Person:
    """Return a new, freshly validated Person with a changed display name.
    The previous name is kept as an alias by default; alias normalization
    and current-name exclusion are handled by the Person model."""
    remove_keys = {normalize_alias(a) for a in aliases_to_remove}
    new_aliases = [a for a in person.aliases if normalize_alias(a) not in remove_keys]
    if retain_previous_as_alias and normalize_alias(person.name) != normalize_alias(name):
        new_aliases.append(person.name)
    new_aliases.extend(aliases_to_add)
    try:
        return Person(**{**person.model_dump(), "name": name, "aliases": new_aliases})
    except PydanticValidationError as exc:
        raise MontaukValidationError(str(exc)) from exc


@asynccontextmanager
async def _scope(
    ctx: Mcp2Context, mcp_ctx: Context, *, capability: str
) -> AsyncIterator[tuple[Session, WorkspaceScope]]:
    """Open a request-scoped session bound to the calling agent's
    workspace, commit on success, and translate repository errors to the
    typed tool errors the MCP layer surfaces to the model."""
    token = extract_bearer_token(dict(getattr(mcp_ctx, "headers", None) or {}))
    with ctx.session_factory() as session:
        cred = authenticate(session, token) if token else None
        if cred is None:
            raise PermissionDeniedError("missing or invalid bearer token")
        if not has_capability(cred, capability):
            raise PermissionDeniedError(f"this token lacks the {capability!r} capability")
        scope = WorkspaceScope(session, cred.workspace_id, Actor("agent", cred.name))
        try:
            yield session, scope
            session.commit()
        except (PersonNotFound, LocalRecordNotFound) as exc:
            session.rollback()
            raise NotFoundError(str(exc)) from exc
        except (RelatedPersonInvalid, PydanticValidationError, ValueError) as exc:
            session.rollback()
            raise MontaukValidationError(str(exc)) from exc
        except BaseException:
            session.rollback()
            raise


def _require(repo: PeopleRepository, person_id: str, *, include_archived: bool = False) -> orm.Person:
    row = repo.get(person_id, include_archived=include_archived)
    if row is None:
        raise NotFoundError(f"no person {person_id!r} in this workspace")
    return row


def _today(as_of: str | None) -> dt.date:
    if as_of is None:
        return dt.date.today()
    try:
        return dt.date.fromisoformat(as_of)
    except ValueError as exc:
        raise MontaukValidationError(f"as_of {as_of!r} is not a valid YYYY-MM-DD date") from exc


# --- write operations (shared by the single tools and update_person_batch) ---


def _op_add_fact(repo: PeopleRepository, row: orm.Person, op: AddFactOp) -> list[str]:
    return [
        repo.add_fact(
            row,
            category=op.category,
            text=op.text,
            date=op.date,
            confidence=op.confidence,
            related_person_id=op.related_person_id,
            sources=list(op.sources) or None,
            reason="agent update",
        )
    ]


def _op_update_fact(repo: PeopleRepository, row: orm.Person, op: UpdateFactOp) -> list[str]:
    repo.update_fact(
        row,
        op.fact_id,
        text=op.text,
        category=op.category,
        date=op.date,
        confidence=op.confidence,
        related_person_id=op.related_person_id,
        reason="agent update",
    )
    return [op.fact_id]


def _op_remove_fact(repo: PeopleRepository, row: orm.Person, op: RemoveFactOp) -> list[str]:
    repo.remove_fact(row, op.fact_id, reason="agent update")
    return [op.fact_id]


def _op_record_interaction(repo: PeopleRepository, row: orm.Person, op: RecordInteractionOp) -> list[str]:
    return [
        repo.add_interaction(
            row,
            date=op.date,
            channel=op.channel,
            connection_level=op.connection_level,
            summary=op.summary,
            sources=list(op.sources) or None,
            reason="agent update",
        )
    ]


def _op_update_contact(repo: PeopleRepository, row: orm.Person, op: UpdateContactDetailsOp) -> list[str]:
    domain = mapping.person_to_domain(row)
    updates = {k: v for k, v in op.model_dump(exclude={"op"}).items() if v is not None}
    new_contact = domain.contact.model_copy(update=updates)
    repo.update_core_fields(row, domain.model_copy(update={"contact": new_contact}), reason="agent update")
    return []


def _op_update_summary(repo: PeopleRepository, row: orm.Person, op: UpdateSummaryOp) -> list[str]:
    domain = mapping.person_to_domain(row)
    repo.update_core_fields(row, domain.model_copy(update={"summary": op.summary}), reason="agent update")
    return []


def _op_set_name(repo: PeopleRepository, row: orm.Person, op: SetNameOp) -> list[str]:
    domain = mapping.person_to_domain(row)
    updated = _apply_name_update(
        domain,
        name=op.name,
        retain_previous_as_alias=op.retain_previous_as_alias,
        aliases_to_add=list(op.aliases_to_add),
        aliases_to_remove=list(op.aliases_to_remove),
    )
    repo.update_core_fields(row, updated, reason="agent update")
    return []


def _op_set_birthday(repo: PeopleRepository, row: orm.Person, op: SetBirthdayOp) -> list[str]:
    domain = mapping.person_to_domain(row)
    value = Birthday.parse(op.birthday) if op.birthday else None
    repo.update_core_fields(row, domain.model_copy(update={"birthday": value}), reason="agent update")
    return []


def _op_set_cadence(repo: PeopleRepository, row: orm.Person, op: SetContactCadenceOp) -> list[str]:
    domain = mapping.person_to_domain(row)
    repo.update_core_fields(
        row,
        domain.model_copy(update={"desired_contact_cadence_days": op.desired_contact_cadence_days}),
        reason="agent update",
    )
    return []


_BATCH_DISPATCH: dict[type, Callable[[PeopleRepository, orm.Person, Any], list[str]]] = {
    AddFactOp: _op_add_fact,
    UpdateFactOp: _op_update_fact,
    RemoveFactOp: _op_remove_fact,
    RecordInteractionOp: _op_record_interaction,
    UpdateContactDetailsOp: _op_update_contact,
    UpdateSummaryOp: _op_update_summary,
    SetNameOp: _op_set_name,
    SetBirthdayOp: _op_set_birthday,
    SetContactCadenceOp: _op_set_cadence,
}


def register_mcp2_tools(server: MCPServer, ctx: Mcp2Context) -> None:
    rcfg = ctx.retrieval_config

    # ------------------------------------------------------------------
    # Retrieval -- briefing first
    # ------------------------------------------------------------------

    @server.tool(
        description=(
            "THE NORMAL RETRIEVAL PATH. Given a known person_id and the user's actual question or "
            "task as `purpose` (e.g. 'brief me before I call Amanda'), return a short factual "
            "briefing written by Montauk's own low-cost model, plus `source_refs` (the fact-N / "
            "int-N records it used), `generated`, and `coverage`. Use the briefing as your grounding "
            "-- do not immediately re-fetch the whole record. A narrow factual purpose ('what is "
            "Amanda's birthday?') is answered directly from the structured field with "
            "`generated: false` and no model call. If `generated` is false with a `status`, a "
            "compact `evidence` packet is included instead. `detail_level`: brief|standard|"
            "comprehensive. `mode`: summary_only (default) | evidence_only | summary_with_evidence."
        )
    )
    async def prepare_person_briefing(
        person_id: str,
        purpose: str,
        mcp_ctx: Context,
        detail_level: str = "standard",
        mode: str = "summary_only",
        max_tokens: int | None = None,
    ) -> dict:
        if not purpose or not purpose.strip():
            raise MontaukValidationError("purpose must not be blank")
        if detail_level not in briefing.COVERAGE_LEVELS:
            raise MontaukValidationError(f"detail_level must be one of {briefing.COVERAGE_LEVELS}")
        if mode not in briefing.MODES:
            raise MontaukValidationError(f"mode must be one of {briefing.MODES}")
        async with _scope(ctx, mcp_ctx, capability=READ) as (session, scope):
            _require(PeopleRepository(scope), person_id, include_archived=True)
            result = await briefing.prepare_briefing(
                session,
                scope,
                public_id=person_id,
                purpose=purpose,
                detail_level=detail_level,
                mode=mode,
                max_tokens=max_tokens,
                secret_box=ctx.secret_box,
                retrieval=ctx.retrieval,
            )
            return result.agent_payload()

    @server.tool(
        description=(
            "Drill down: return the underlying records for `source_refs` taken from a briefing "
            "(fact-N / int-N, or a 'field:<name>' ref). Use this to verify or quote exact wording, "
            "or to see detail the briefing left out. Refs resolve only against this person's own "
            "record; unknown refs come back in `missing_refs`."
        )
    )
    async def get_context_sources(person_id: str, source_refs: list[str], mcp_ctx: Context) -> dict:
        async with _scope(ctx, mcp_ctx, capability=READ) as (_session, scope):
            return briefing.get_context_sources(scope, public_id=person_id, source_refs=source_refs)

    @server.tool(
        description=(
            "Deterministic, no-LLM retrieval: return the ranked raw facts, relationships, and "
            "interactions relevant to `purpose` within a token budget, with no synthesized prose. "
            "Prefer prepare_person_briefing for an ordinary question; use this when you want the raw "
            "evidence yourself, or as the fallback when briefings are unavailable. "
            f"`detail_level`: {', '.join(DETAIL_LEVELS)}."
        )
    )
    async def prepare_person_context(
        person_id: str,
        purpose: str,
        mcp_ctx: Context,
        detail_level: str = "standard",
        max_tokens: int | None = None,
    ) -> PersonContextResponse:
        if not purpose or not purpose.strip():
            raise MontaukValidationError("purpose must not be blank")
        if detail_level not in DETAIL_LEVELS:
            raise MontaukValidationError(f"detail_level must be one of {DETAIL_LEVELS}")
        budget = max_tokens if max_tokens is not None else rcfg.budget_for(detail_level)
        if not (rcfg.min_tokens <= budget <= rcfg.max_tokens):
            raise MontaukValidationError(f"max_tokens must be within [{rcfg.min_tokens}, {rcfg.max_tokens}]")
        async with _scope(ctx, mcp_ctx, capability=READ) as (_session, scope):
            row = _require(PeopleRepository(scope), person_id, include_archived=True)
            result = build_person_context(
                mapping.person_to_domain(row),
                purpose,
                detail_level=detail_level,
                budget_tokens=budget,
                semantic_index=None,
                lexical_enabled=rcfg.lexical_enabled,
            )
            return PersonContextResponse.model_validate(result.to_payload())

    # ------------------------------------------------------------------
    # Identity & plain reads
    # ------------------------------------------------------------------

    @server.tool(
        description=(
            "Find people by name, alias, company, location, or contact detail. Returns candidates "
            "with their person_id -- resolve identity with this before any other tool. If several "
            "plausible people come back, ask the user rather than guessing."
        )
    )
    async def search_people(query: str, mcp_ctx: Context) -> SearchResult:
        async with _scope(ctx, mcp_ctx, capability=READ) as (_session, scope):
            rows = PeopleRepository(scope).list_people(query=query, archived=False, limit=ctx.max_candidates)
            return SearchResult(
                candidates=[
                    PersonCandidate(
                        person_id=r.public_id,
                        name=r.name,
                        summary=r.summary,
                        match_evidence=[a.alias for a in r.aliases] or [],
                    )
                    for r in rows
                ]
            )

    @server.tool(
        description="Return the structured fields for a known person_id (name, aliases, birthday, "
        "location, company, job title, contact cadence, contact methods, profile summary). No facts "
        "or interactions -- use prepare_person_briefing or get_facts for those."
    )
    async def get_person(person_id: str, mcp_ctx: Context) -> PersonCore:
        async with _scope(ctx, mcp_ctx, capability=READ) as (_session, scope):
            row = _require(PeopleRepository(scope), person_id, include_archived=True)
            return _person_core(mapping.person_to_domain(row))

    @server.tool(
        description=(
            f"Return facts for a known person_id, optionally one category ({', '.join(CATEGORIES)}). "
            "Use to verify or expand on a briefing's source_refs."
        )
    )
    async def get_facts(person_id: str, mcp_ctx: Context, category: str | None = None) -> list[Fact]:
        if category is not None and category not in CATEGORIES:
            raise MontaukValidationError(f"category must be one of {CATEGORIES}")
        async with _scope(ctx, mcp_ctx, capability=READ) as (_session, scope):
            row = _require(PeopleRepository(scope), person_id, include_archived=True)
            facts = mapping.person_to_domain(row).facts
            return [f for f in facts if category is None or f.category == category]

    @server.tool(
        description="Return interactions for a known person_id, most recent first, optionally "
        "limited to the last `limit`. Use to verify or expand on a briefing's source_refs."
    )
    async def get_interactions(
        person_id: str, mcp_ctx: Context, limit: int | None = None
    ) -> list[Interaction]:
        async with _scope(ctx, mcp_ctx, capability=READ) as (_session, scope):
            row = _require(PeopleRepository(scope), person_id, include_archived=True)
            items = sorted(
                mapping.person_to_domain(row).interactions,
                key=lambda i: i.date.latest(),
                reverse=True,
            )
            return items[:limit] if limit else items

    @server.tool(
        description=(
            "Return the complete canonical record for a known person_id as Markdown -- every field, "
            "fact, and interaction. Use only for explicit review, export, or maintenance; for an "
            "ordinary question use prepare_person_briefing, which is far smaller."
        )
    )
    async def get_full_record(person_id: str, mcp_ctx: Context) -> str:
        async with _scope(ctx, mcp_ctx, capability=READ) as (_session, scope):
            row = _require(PeopleRepository(scope), person_id, include_archived=True)
            return person_to_markdown(mapping.person_to_domain(row))

    @server.tool(
        description="List active people (person_id, name, one-line summary), name order. For a "
        "quick roster; use search_people to resolve a specific person."
    )
    async def list_people(mcp_ctx: Context) -> list[PersonCandidate]:
        async with _scope(ctx, mcp_ctx, capability=READ) as (_session, scope):
            rows = PeopleRepository(scope).list_people(archived=False)
            return [PersonCandidate(person_id=r.public_id, name=r.name, summary=r.summary) for r in rows]

    @server.tool(
        description="Active people with a birthday in the next `within_days` days (default 30), "
        "soonest first. A birthday with no known year is still included. Deterministic -- reports "
        "dates, sends nothing."
    )
    async def get_upcoming_birthdays(
        mcp_ctx: Context, within_days: int = 30, as_of: str | None = None
    ) -> list[UpcomingBirthday]:
        if within_days < 0:
            raise MontaukValidationError("within_days must be >= 0")
        today = _today(as_of)
        out: list[UpcomingBirthday] = []
        async with _scope(ctx, mcp_ctx, capability=READ) as (_session, scope):
            for row in PeopleRepository(scope).list_people(archived=False):
                if row.birthday_month is None:
                    continue
                b = Birthday(month=row.birthday_month, day=row.birthday_day, year=row.birthday_year)
                occ = b.next_occurrence(today)
                days = (occ - today).days
                if days <= within_days:
                    out.append(
                        UpcomingBirthday(
                            person_id=row.public_id,
                            name=row.name,
                            birthday=b.to_string(),
                            next_occurrence=occ.isoformat(),
                            days_until=days,
                        )
                    )
        out.sort(key=lambda u: u.days_until)
        return out

    @server.tool(
        description="Active people overdue for contact relative to their desired_contact_cadence_days. "
        "Someone with a cadence but no recorded interaction is returned as 'never_contacted'. People "
        "with no cadence set are never returned."
    )
    async def list_overdue_contacts(mcp_ctx: Context, as_of: str | None = None) -> list[OverdueContact]:
        today = _today(as_of)
        out: list[OverdueContact] = []
        async with _scope(ctx, mcp_ctx, capability=READ) as (_session, scope):
            for row in PeopleRepository(scope).list_people(archived=False):
                cadence = row.desired_contact_cadence_days
                if cadence is None:
                    continue
                domain = mapping.person_to_domain(row)
                last = max((i.date.latest() for i in domain.interactions), default=None)
                if last is None:
                    out.append(
                        OverdueContact(
                            person_id=row.public_id,
                            name=row.name,
                            desired_contact_cadence_days=cadence,
                            last_interaction_at=None,
                            days_since_last_interaction=None,
                            status="never_contacted",
                        )
                    )
                    continue
                days_since = (today - last).days
                if days_since >= cadence:
                    out.append(
                        OverdueContact(
                            person_id=row.public_id,
                            name=row.name,
                            desired_contact_cadence_days=cadence,
                            last_interaction_at=last.isoformat(),
                            days_since_last_interaction=days_since,
                            status="overdue",
                        )
                    )
        out.sort(key=lambda o: (o.status != "never_contacted", -(o.days_since_last_interaction or 10**9)))
        return out

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    @server.tool(
        description=(
            "Create a new person record. Call search_people first and do not create a duplicate "
            "when identity is uncertain -- ask the user. Returns the new person_id."
        )
    )
    async def create_person(
        name: str,
        mcp_ctx: Context,
        summary: str | None = None,
        birthday: str | None = None,
        location: str | None = None,
        company: str | None = None,
        job_title: str | None = None,
    ) -> WriteResult:
        async with _scope(ctx, mcp_ctx, capability=WRITE) as (_session, scope):
            repo = PeopleRepository(scope)
            public_id = repo.allocate_public_id()
            repo.create(
                Person(
                    id=public_id,
                    name=name,
                    summary=summary or None,
                    birthday=Birthday.parse(birthday) if birthday else None,
                    location=location or None,
                    company=company or None,
                    job_title=job_title or None,
                ),
                authority="agent_curated",
            )
            return WriteResult(person_id=public_id)

    @server.tool(
        description=(
            f"Add one fact to a known person_id. category is one of {', '.join(CATEGORIES)}. Use "
            "confidence 'high' for directly stated facts, 'medium'/'low' for inference. Set "
            "related_person_id only when the fact is genuinely about this person's relationship or "
            "interaction with that other (existing) person."
        )
    )
    async def add_fact(
        person_id: str,
        category: str,
        text: str,
        mcp_ctx: Context,
        date: str | None = None,
        confidence: str = "high",
        related_person_id: str | None = None,
    ) -> WriteResult:
        async with _scope(ctx, mcp_ctx, capability=WRITE) as (_session, scope):
            repo = PeopleRepository(scope)
            row = _require(repo, person_id)
            ids = _op_add_fact(
                repo,
                row,
                AddFactOp(
                    category=category,
                    text=text,
                    date=date,
                    confidence=confidence,
                    related_person_id=related_person_id,
                ),
            )
            return WriteResult(person_id=person_id, changed_ids=ids)

    @server.tool(
        description="Update one existing fact (by fact_id) on a known person_id. Only the fields "
        "you pass change. Correcting a fact is not erasing history."
    )
    async def update_fact(
        person_id: str,
        fact_id: str,
        mcp_ctx: Context,
        text: str | None = None,
        category: str | None = None,
        date: str | None = None,
        confidence: str | None = None,
        related_person_id: str | None = None,
    ) -> WriteResult:
        async with _scope(ctx, mcp_ctx, capability=WRITE) as (_session, scope):
            repo = PeopleRepository(scope)
            row = _require(repo, person_id)
            ids = _op_update_fact(
                repo,
                row,
                UpdateFactOp(
                    fact_id=fact_id,
                    text=text,
                    category=category,
                    date=date,
                    confidence=confidence,
                    related_person_id=related_person_id,
                ),
            )
            return WriteResult(person_id=person_id, changed_ids=ids)

    @server.tool(
        description="Remove a fact (by fact_id) from a known person_id. Use only when the fact is "
        "wrong or was never true -- not merely because it is old or inconvenient."
    )
    async def remove_fact(person_id: str, fact_id: str, mcp_ctx: Context) -> WriteResult:
        async with _scope(ctx, mcp_ctx, capability=WRITE) as (_session, scope):
            repo = PeopleRepository(scope)
            row = _require(repo, person_id)
            _op_remove_fact(repo, row, RemoveFactOp(fact_id=fact_id))
            return WriteResult(person_id=person_id, changed_ids=[fact_id])

    @server.tool(
        description=(
            "Record an interaction with a known person_id: a date (YYYY-MM-DD, or a partial date), "
            "optional channel, connection_level 1-6, and a concise summary. Record a meaningful "
            "interaction even if it produced no new facts."
        )
    )
    async def record_interaction(
        person_id: str,
        date: str,
        mcp_ctx: Context,
        channel: str | None = None,
        connection_level: int | None = None,
        summary: str | None = None,
    ) -> WriteResult:
        async with _scope(ctx, mcp_ctx, capability=WRITE) as (_session, scope):
            repo = PeopleRepository(scope)
            row = _require(repo, person_id)
            ids = _op_record_interaction(
                repo,
                row,
                RecordInteractionOp(
                    date=date, channel=channel, connection_level=connection_level, summary=summary
                ),
            )
            return WriteResult(person_id=person_id, changed_ids=ids)

    @server.tool(
        description="Update an existing interaction (by interaction_id) on a known person_id. Only "
        "the fields you pass change."
    )
    async def update_interaction(
        person_id: str,
        interaction_id: str,
        mcp_ctx: Context,
        date: str | None = None,
        channel: str | None = None,
        connection_level: int | None = None,
        summary: str | None = None,
    ) -> WriteResult:
        async with _scope(ctx, mcp_ctx, capability=WRITE) as (_session, scope):
            repo = PeopleRepository(scope)
            row = _require(repo, person_id)
            repo.update_interaction(
                row,
                interaction_id,
                date=date,
                channel=channel,
                connection_level=connection_level,
                summary=summary,
                reason="agent update",
            )
            return WriteResult(person_id=person_id, changed_ids=[interaction_id])

    @server.tool(
        description="Remove an interaction (by interaction_id) from a known person_id. Use only "
        "when the interaction record itself is erroneous or never occurred."
    )
    async def remove_interaction(person_id: str, interaction_id: str, mcp_ctx: Context) -> WriteResult:
        async with _scope(ctx, mcp_ctx, capability=WRITE) as (_session, scope):
            repo = PeopleRepository(scope)
            row = _require(repo, person_id)
            repo.remove_interaction(row, interaction_id, reason="agent update")
            return WriteResult(person_id=person_id, changed_ids=[interaction_id])

    @server.tool(
        description=(
            "Move an interaction attributed to the wrong person: removed from from_person_id and "
            "re-recorded on to_person_id (both must exist), so the wrong person keeps no trace. "
            "Returns the new interaction_id on the target."
        )
    )
    async def reattribute_interaction(
        from_person_id: str, interaction_id: str, to_person_id: str, mcp_ctx: Context
    ) -> WriteResult:
        async with _scope(ctx, mcp_ctx, capability=WRITE) as (_session, scope):
            repo = PeopleRepository(scope)
            src = _require(repo, from_person_id)
            dst = _require(repo, to_person_id)
            moved = next(
                (i for i in mapping.person_to_domain(src).interactions if i.id == interaction_id), None
            )
            if moved is None:
                raise NotFoundError(f"interaction {interaction_id!r} not on {from_person_id}")
            repo.remove_interaction(src, interaction_id, reason="reattributed to another person")
            new_id = repo.add_interaction(
                dst,
                date=moved.date.to_string(),
                channel=moved.channel,
                connection_level=moved.connection_level,
                summary=moved.summary,
                sources=list(moved.sources) or None,
                reason=f"reattributed from {from_person_id}",
            )
            return WriteResult(person_id=to_person_id, changed_ids=[new_id])

    @server.tool(
        description="Set or clear contact details on a known person_id (emails, phones, address). "
        "Passing a list replaces that list; pass null to leave it unchanged."
    )
    async def update_contact_details(
        person_id: str,
        mcp_ctx: Context,
        emails: list[str] | None = None,
        phones: list[str] | None = None,
        address: str | None = None,
    ) -> WriteResult:
        async with _scope(ctx, mcp_ctx, capability=WRITE) as (_session, scope):
            repo = PeopleRepository(scope)
            row = _require(repo, person_id)
            _op_update_contact(
                repo, row, UpdateContactDetailsOp(emails=emails, phones=phones, address=address)
            )
            return WriteResult(person_id=person_id)

    @server.tool(description="Replace the one-line profile summary on a known person_id.")
    async def update_summary(person_id: str, summary: str, mcp_ctx: Context) -> WriteResult:
        async with _scope(ctx, mcp_ctx, capability=WRITE) as (_session, scope):
            repo = PeopleRepository(scope)
            row = _require(repo, person_id)
            _op_update_summary(repo, row, UpdateSummaryOp(summary=summary))
            return WriteResult(person_id=person_id)

    @server.tool(
        description=(
            "Change a person's display name, keeping the previous name as an alias by default. "
            "Never archive and recreate a person to fix a name."
        )
    )
    async def update_person_name(
        person_id: str, name: str, mcp_ctx: Context, retain_previous_as_alias: bool = True
    ) -> WriteResult:
        async with _scope(ctx, mcp_ctx, capability=WRITE) as (_session, scope):
            repo = PeopleRepository(scope)
            row = _require(repo, person_id)
            _op_set_name(repo, row, SetNameOp(name=name, retain_previous_as_alias=retain_previous_as_alias))
            return WriteResult(person_id=person_id)

    @server.tool(
        description="Set or clear a person's birthday. Partial precision is fine: 'YYYY-MM-DD', "
        "'YYYY-MM', 'MM-DD', or 'MM'. Pass null to clear."
    )
    async def set_birthday(person_id: str, birthday: str | None, mcp_ctx: Context) -> WriteResult:
        async with _scope(ctx, mcp_ctx, capability=WRITE) as (_session, scope):
            repo = PeopleRepository(scope)
            row = _require(repo, person_id)
            _op_set_birthday(repo, row, SetBirthdayOp(birthday=birthday))
            return WriteResult(person_id=person_id)

    @server.tool(
        description="Set or clear the desired contact cadence (days) for a known person_id. This "
        "opts them into list_overdue_contacts. Pass null to clear."
    )
    async def set_contact_cadence(person_id: str, days: int | None, mcp_ctx: Context) -> WriteResult:
        async with _scope(ctx, mcp_ctx, capability=WRITE) as (_session, scope):
            repo = PeopleRepository(scope)
            row = _require(repo, person_id)
            _op_set_cadence(repo, row, SetContactCadenceOp(desired_contact_cadence_days=days))
            return WriteResult(person_id=person_id)

    @server.tool(
        description=(
            "Atomically apply several related changes to ONE known person_id in one call: any mix "
            "of add_fact, update_fact, remove_fact, record_interaction, update_contact_details, "
            "update_summary, set_name, set_birthday, set_contact_cadence. If any operation is "
            "invalid, none are applied. Prefer this when one event produces several updates for one "
            "person."
        )
    )
    async def update_person_batch(
        person_id: str, operations: list[BatchOperation], mcp_ctx: Context
    ) -> WriteResult:
        if not operations:
            raise MontaukValidationError("operations must not be empty")
        async with _scope(ctx, mcp_ctx, capability=WRITE) as (_session, scope):
            repo = PeopleRepository(scope)
            row = _require(repo, person_id)
            changed: list[str] = []
            for op in operations:
                handler = _BATCH_DISPATCH.get(type(op))
                if handler is None:  # pragma: no cover - guarded by the union type
                    raise MontaukValidationError(f"unsupported batch operation: {type(op).__name__}")
                changed.extend(handler(repo, row, op))
            return WriteResult(person_id=person_id, changed_ids=changed)

    @server.tool(
        description="Archive a known person_id: removes them from active search / briefing / "
        "birthday / cadence results without deleting the record. Reversible via restore_person."
    )
    async def archive_person(person_id: str, mcp_ctx: Context) -> WriteResult:
        async with _scope(ctx, mcp_ctx, capability=WRITE) as (_session, scope):
            repo = PeopleRepository(scope)
            row = _require(repo, person_id, include_archived=True)
            repo.set_archived(row, True, reason="agent archive")
            return WriteResult(person_id=person_id)

    @server.tool(description="Restore a previously archived person_id to active status.")
    async def restore_person(person_id: str, mcp_ctx: Context) -> WriteResult:
        async with _scope(ctx, mcp_ctx, capability=WRITE) as (_session, scope):
            repo = PeopleRepository(scope)
            row = _require(repo, person_id, include_archived=True)
            repo.set_archived(row, False, reason="agent restore")
            return WriteResult(person_id=person_id)
