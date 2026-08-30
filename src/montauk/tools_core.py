"""Core identity/read/write MCP tools (spec sections 21, 23, 24, 25).

Every mutation tool builds and validates the full new Person, then goes
through WriteQueue.submit() so all writes stay serialized and each
mutation either fully applies or doesn't happen at all. Read tools stay
unserialized (broad reads are allowed to run concurrently; spec section
23).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from mcp.server.mcpserver import MCPServer
from pydantic import ValidationError as PydanticValidationError

from .errors import ArchivedError, MontaukValidationError, NotFoundError
from .ids import next_fact_id, next_interaction_id, next_person_id
from .markdown_store import MarkdownStore, PersonNotFoundError, person_to_markdown
from .models import ContactInfo, Fact, Interaction, Person, Source
from .schema import CATEGORIES
from .sqlite_index import SqliteIndex, compute_content_hash
from .tool_types import IndexUpdateStatus, PersonCandidate, PersonCore, SearchResult, WriteResult
from .write_queue import WriteQueue

logger = logging.getLogger(__name__)


@dataclass
class MontaukContext:
    store: MarkdownStore
    sqlite_index: SqliteIndex
    write_queue: WriteQueue


def _load_person_or_error(store: MarkdownStore, person_id: str) -> Person:
    try:
        return store.read_person(person_id)
    except PersonNotFoundError:
        if store.is_archived(person_id):
            raise ArchivedError(
                f"person {person_id!r} is archived; use get_archived_person or list_archived_people"
            ) from None
        raise NotFoundError(f"person {person_id!r} not found") from None


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


def _write_and_index(ctx: MontaukContext, person: Person) -> IndexUpdateStatus:
    """Write canonical Markdown first (Appendix B); a derived-index failure
    afterward is logged and reported as degraded, never rolled back."""
    ctx.store.write_person(person)
    try:
        path = ctx.store.person_path(person.id)
        ctx.sqlite_index.upsert_person(person, file_path=str(path), content_hash=compute_content_hash(path))
        return "ok"
    except Exception:
        logger.exception("failed to update derived index for person %r", person.id)
        return "degraded"


def _replace_field(person: Person, **updates: object) -> Person:
    """Build a new, freshly-validated Person with the given fields replaced.
    Round-trips through model_dump()/the constructor rather than
    model_copy(), so field-level and cross-field validators (e.g. the
    duplicate-fact-id check) re-run on the result."""
    try:
        return Person(**{**person.model_dump(), **updates})
    except PydanticValidationError as exc:
        raise MontaukValidationError(str(exc)) from exc


def register_core_tools(server: MCPServer, ctx: MontaukContext) -> None:
    store, sqlite_index, write_queue = ctx.store, ctx.sqlite_index, ctx.write_queue

    @server.tool(
        description=(
            "Search active relationship memory using a name, alias, employer, location, or other "
            "remembered detail. Accepts vague identifying details, not just exact names. Returns "
            "ranked candidates with match_evidence explaining why each matched. Multiple candidates "
            "are expected when identity is ambiguous -- do not guess; ask the user to clarify rather "
            "than picking one. Phase 1 matching is exact/substring only (semantic recall of vaguer "
            "descriptions is added in a later step)."
        )
    )
    async def search_people(query: str) -> SearchResult:
        needle = query.strip().lower()
        if not needle:
            return SearchResult(candidates=[])
        candidates: list[PersonCandidate] = []
        for row in sqlite_index.list_all():
            aliases: list[str] = json.loads(row["aliases_json"])
            summary = row["summary"]
            evidence: list[str] = []
            if needle in row["name"].lower():
                evidence.append(f"name matches {query!r}")
            for alias in aliases:
                if needle in alias.lower():
                    evidence.append(f"alias {alias!r} matches {query!r}")
            if summary and needle in summary.lower():
                evidence.append("summary mentions the query")
            if row["company"] and needle in row["company"].lower():
                evidence.append(f"company {row['company']!r} matches {query!r}")
            if row["location"] and needle in row["location"].lower():
                evidence.append(f"location matches {query!r}")
            if evidence:
                candidates.append(
                    PersonCandidate(
                        person_id=row["person_id"], name=row["name"], summary=summary, match_evidence=evidence
                    )
                )
        return SearchResult(candidates=candidates)

    @server.tool(
        description=(
            "Return structured core fields (name, aliases, birthday, location, company, job_title, "
            "cadence, summary, contact) for a known person_id. Does not include facts or interactions "
            "-- use get_facts/get_interactions/get_full_record for those. Prefer this over "
            "get_full_record when you only need the core profile."
        )
    )
    async def get_person(person_id: str) -> PersonCore:
        return _person_core(_load_person_or_error(store, person_id))

    @server.tool(
        description=(
            "Return the complete canonical Markdown record for a known person_id, including every "
            "fact and interaction. Use intentionally: this returns substantially more content than "
            "get_person/get_facts/get_interactions and consumes more model context. Prefer targeted "
            "retrieval tools for narrow questions."
        )
    )
    async def get_full_record(person_id: str) -> str:
        return person_to_markdown(_load_person_or_error(store, person_id))

    @server.tool(
        description=(
            "Return facts for a known person_id, optionally narrowed to one fixed category "
            f"({', '.join(CATEGORIES)}). Omit category to return all facts."
        )
    )
    async def get_facts(person_id: str, category: str | None = None) -> list[Fact]:
        person = _load_person_or_error(store, person_id)
        if category is not None and category not in CATEGORIES:
            raise MontaukValidationError(f"category {category!r} is not one of the fixed categories: {CATEGORIES}")
        if category is None:
            return person.facts
        return [f for f in person.facts if f.category == category]

    @server.tool(
        description=(
            "Return interaction history for a known person_id, most recent first. Pass `limit` to "
            "return only the N most recent interactions instead of the full history."
        )
    )
    async def get_interactions(person_id: str, limit: int | None = None) -> list[Interaction]:
        person = _load_person_or_error(store, person_id)
        ordered = sorted(person.interactions, key=lambda i: i.date.latest(), reverse=True)
        return ordered[:limit] if limit is not None else ordered

    @server.tool(
        description=(
            "Create a new person record. Search first (search_people) if the person might already "
            "exist -- do not use this tool to resolve identity uncertainty; if search returns "
            "plausible candidates, ask the user to clarify instead of creating a duplicate. The "
            "returned person_id is permanent and should be used for all subsequent operations on "
            "this person."
        )
    )
    async def create_person(
        name: str,
        aliases: list[str] | None = None,
        birthday: str | None = None,
        location: str | None = None,
        company: str | None = None,
        job_title: str | None = None,
        desired_contact_cadence_days: int | None = None,
        summary: str | None = None,
    ) -> WriteResult:
        def op() -> WriteResult:
            existing_ids = set(store.list_person_ids()) | set(store.list_archived_person_ids())
            person_id = next_person_id(name, existing_ids)
            try:
                person = Person(
                    id=person_id,
                    name=name,
                    aliases=aliases or [],
                    birthday=birthday,
                    location=location,
                    company=company,
                    job_title=job_title,
                    desired_contact_cadence_days=desired_contact_cadence_days,
                    summary=summary,
                )
            except PydanticValidationError as exc:
                raise MontaukValidationError(str(exc)) from exc
            status = _write_and_index(ctx, person)
            return WriteResult(person_id=person.id, index_update_status=status)

        return await write_queue.submit(op)

    @server.tool(
        description=(
            "Add a new fact to a known person_id in one fixed category "
            f"({', '.join(CATEGORIES)}). Use high confidence for directly stated or strongly "
            "supported facts; medium/low for genuine inference or uncertainty. Set "
            "related_person_id only when this fact concerns another existing person by ID."
        )
    )
    async def add_fact(
        person_id: str,
        category: str,
        text: str,
        date: str | None = None,
        confidence: str = "high",
        related_person_id: str | None = None,
        sources: list[Source] | None = None,
    ) -> WriteResult:
        def op() -> WriteResult:
            person = _load_person_or_error(store, person_id)
            fact_id = next_fact_id(person.fact_ids())
            try:
                new_fact = Fact(
                    id=fact_id,
                    category=category,
                    text=text,
                    date=date,
                    confidence=confidence,
                    related_person_id=related_person_id,
                    sources=sources or [],
                )
            except PydanticValidationError as exc:
                raise MontaukValidationError(str(exc)) from exc
            updated = _replace_field(person, facts=[*person.facts, new_fact])
            status = _write_and_index(ctx, updated)
            return WriteResult(person_id=person_id, changed_ids=[fact_id], index_update_status=status)

        return await write_queue.submit(op)

    @server.tool(
        description=(
            "Update fields on an existing fact by person_id and fact_id. Only fields you pass are "
            "changed; omitted fields keep their current value (this tool cannot clear a field to "
            "empty -- use remove_fact and add_fact for that)."
        )
    )
    async def update_fact(
        person_id: str,
        fact_id: str,
        text: str | None = None,
        category: str | None = None,
        date: str | None = None,
        confidence: str | None = None,
        related_person_id: str | None = None,
    ) -> WriteResult:
        def op() -> WriteResult:
            person = _load_person_or_error(store, person_id)
            existing = person.get_fact(fact_id)
            if existing is None:
                raise NotFoundError(f"fact {fact_id!r} not found on person {person_id!r}")
            field_updates = {
                k: v
                for k, v in {
                    "text": text,
                    "category": category,
                    "date": date,
                    "confidence": confidence,
                    "related_person_id": related_person_id,
                }.items()
                if v is not None
            }
            try:
                new_fact = Fact(**{**existing.model_dump(), **field_updates})
            except PydanticValidationError as exc:
                raise MontaukValidationError(str(exc)) from exc
            new_facts = [new_fact if f.id == fact_id else f for f in person.facts]
            updated = _replace_field(person, facts=new_facts)
            status = _write_and_index(ctx, updated)
            return WriteResult(person_id=person_id, changed_ids=[fact_id], index_update_status=status)

        return await write_queue.submit(op)

    @server.tool(
        description=(
            "Remove a fact from a person's record by person_id and fact_id. Incorrect facts should "
            "be corrected or removed rather than kept as superseded misinformation -- Git history "
            "preserves the record of the change."
        )
    )
    async def remove_fact(person_id: str, fact_id: str) -> WriteResult:
        def op() -> WriteResult:
            person = _load_person_or_error(store, person_id)
            if person.get_fact(fact_id) is None:
                raise NotFoundError(f"fact {fact_id!r} not found on person {person_id!r}")
            updated = _replace_field(person, facts=[f for f in person.facts if f.id != fact_id])
            status = _write_and_index(ctx, updated)
            return WriteResult(person_id=person_id, changed_ids=[fact_id], index_update_status=status)

        return await write_queue.submit(op)

    @server.tool(
        description=(
            "Record a concise interaction summary for a known person_id. Worth calling even when the "
            "interaction produced no new facts -- interactions drive contact-recency/cadence queries "
            "regardless. Do not pass a raw transcript or message thread; summarize concisely."
        )
    )
    async def record_interaction(
        person_id: str,
        date: str,
        channel: str | None = None,
        connection_level: int | None = None,
        summary: str | None = None,
        sources: list[Source] | None = None,
    ) -> WriteResult:
        def op() -> WriteResult:
            person = _load_person_or_error(store, person_id)
            interaction_id = next_interaction_id(person.interaction_ids())
            try:
                new_interaction = Interaction(
                    id=interaction_id,
                    date=date,
                    channel=channel,
                    connection_level=connection_level,
                    summary=summary,
                    sources=sources or [],
                )
            except PydanticValidationError as exc:
                raise MontaukValidationError(str(exc)) from exc
            updated = _replace_field(person, interactions=[*person.interactions, new_interaction])
            status = _write_and_index(ctx, updated)
            return WriteResult(person_id=person_id, changed_ids=[interaction_id], index_update_status=status)

        return await write_queue.submit(op)

    @server.tool(
        description=(
            "Update current contact details (emails, phones, address, messaging handles) for a known "
            "person_id. Only the fields you pass are replaced; omitted fields keep their current "
            "value. This represents currently-valid contact info only -- obsolete details belong in a "
            "narrative fact, not here."
        )
    )
    async def update_contact_details(
        person_id: str,
        emails: list[str] | None = None,
        phones: list[str] | None = None,
        address: str | None = None,
        messaging: dict[str, str] | None = None,
    ) -> WriteResult:
        def op() -> WriteResult:
            person = _load_person_or_error(store, person_id)
            contact_updates = {
                k: v
                for k, v in {
                    "emails": emails,
                    "phones": phones,
                    "address": address,
                    "messaging": messaging,
                }.items()
                if v is not None
            }
            try:
                new_contact = ContactInfo(**{**person.contact.model_dump(), **contact_updates})
            except PydanticValidationError as exc:
                raise MontaukValidationError(str(exc)) from exc
            updated = _replace_field(person, contact=new_contact)
            status = _write_and_index(ctx, updated)
            return WriteResult(person_id=person_id, index_update_status=status)

        return await write_queue.submit(op)

    @server.tool(description="Replace the short identifying summary for a known person_id.")
    async def update_summary(person_id: str, summary: str) -> WriteResult:
        def op() -> WriteResult:
            person = _load_person_or_error(store, person_id)
            updated = _replace_field(person, summary=summary)
            status = _write_and_index(ctx, updated)
            return WriteResult(person_id=person_id, index_update_status=status)

        return await write_queue.submit(op)

    @server.tool(
        description=(
            "Set or clear a known person_id's birthday. Accepts YYYY-MM-DD (year known) or MM-DD "
            "(year unknown). Pass null to clear an existing birthday."
        )
    )
    async def set_birthday(person_id: str, birthday: str | None) -> WriteResult:
        def op() -> WriteResult:
            person = _load_person_or_error(store, person_id)
            updated = _replace_field(person, birthday=birthday)
            status = _write_and_index(ctx, updated)
            return WriteResult(person_id=person_id, index_update_status=status)

        return await write_queue.submit(op)

    @server.tool(
        description=(
            "Set or clear a known person_id's desired proactive keep-in-touch cadence, in days. Pass "
            "null to indicate no proactive keep-in-touch priority."
        )
    )
    async def set_contact_cadence(person_id: str, desired_contact_cadence_days: int | None) -> WriteResult:
        def op() -> WriteResult:
            person = _load_person_or_error(store, person_id)
            updated = _replace_field(person, desired_contact_cadence_days=desired_contact_cadence_days)
            status = _write_and_index(ctx, updated)
            return WriteResult(person_id=person_id, index_update_status=status)

        return await write_queue.submit(op)
