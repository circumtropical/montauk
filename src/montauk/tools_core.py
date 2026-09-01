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

from mcp.server.mcpserver import Context, MCPServer
from pydantic import ValidationError as PydanticValidationError

from .auth import AgentIdentity, CredentialStore, resolve_http_identity, require_write
from .errors import ArchivedError, MontaukValidationError, NotFoundError
from .ids import PERSON_ID_RE, next_fact_id, next_interaction_id, next_person_id
from .markdown_store import MarkdownStore, PersonNotFoundError, person_to_markdown
from .models import ContactInfo, Fact, Interaction, Person, Source
from .schema import CATEGORIES
from .semantic_index import SemanticIndex
from .sqlite_index import SqliteIndex, compute_content_hash
from .tool_types import (
    AddFactOp,
    BatchOperation,
    IndexUpdateStatus,
    PersonCandidate,
    PersonCore,
    RecordInteractionOp,
    RemoveFactOp,
    SearchResult,
    SetBirthdayOp,
    SetContactCadenceOp,
    UpdateContactDetailsOp,
    UpdateFactOp,
    UpdateSummaryOp,
    WriteResult,
)
from .write_queue import WriteQueue

logger = logging.getLogger(__name__)

# Record-scoping guidance (spec section 15). A source may be about several
# people, but a Montauk record is about exactly one. These strings are
# spliced into the model-visible descriptions of every mutation tool that
# accepts narrative content, so an agent that never reads the server
# instructions still sees the locally relevant part of the rule.
RECORD_SCOPING_RULE = (
    "Content must be directly relevant to the specified person_id. Do not include people "
    "or details merely because they appeared in the same conversation or source event. "
    "Cross-person references are appropriate only when the source establishes a direct "
    "relationship or interaction, or when the reference is necessary to understand a fact "
    "directly about the person of record."
)
BATCH_SEPARATION_RULE = (
    "If one source contains information about multiple unrelated people, create a separate "
    "batch for each person and include only the operations relevant to that person."
)
INTERACTION_SCOPING_RULE = (
    "An interaction summary should describe the person of record's participation and only "
    "the context necessary to understand that interaction. Other participants may be named "
    "only when the person of record actually interacted with them or has a relevant "
    "relationship with them. Unrelated people discussed in the source material must not be "
    "included."
)


@dataclass
class MontaukContext:
    store: MarkdownStore
    sqlite_index: SqliteIndex
    write_queue: WriteQueue
    # Semantic search is an optional enhancement layer (spec section 31's
    # search.semantic_enabled config flag): None means exact/substring
    # matching only, which is what every pre-existing test still exercises
    # without paying to construct an embedding model.
    semantic_index: SemanticIndex | None = None
    # Auth is likewise opt-in (spec section 30): credential_store=None
    # means enforcement is off entirely (e.g. a bare local/trusted-user
    # deployment, or the ~200 pre-auth tests). When set, every mutation
    # tool requires a resolved read_write identity -- from the HTTP
    # request's Authorization header per call, or from stdio_identity
    # (resolved once at process startup from an env var) when there is
    # no per-request header at all (stdio has exactly one client).
    credential_store: CredentialStore | None = None
    stdio_identity: AgentIdentity | None = None
    max_candidates: int = 5
    # Empirically calibrated for the default local model (all-MiniLM-L6-v2),
    # not copied from the spec's illustrative config.example.yaml value: for
    # this model, clearly-related short-text pairs typically score
    # ~0.45-0.75 cosine similarity while unrelated pairs score ~-0.1-0.05, so
    # 0.55 (the spec's example) silently dropped genuinely relevant matches.
    similarity_threshold: float = 0.35


def _load_person_or_error(store: MarkdownStore, person_id: str) -> Person:
    try:
        return store.read_person(person_id)
    except PersonNotFoundError:
        if store.is_archived(person_id):
            raise ArchivedError(
                f"person {person_id!r} is archived; use get_archived_person or list_archived_people"
            ) from None
        raise NotFoundError(f"person {person_id!r} not found") from None


def _validate_related_person_id(
    ctx: MontaukContext, owner_person_id: str, related_person_id: str | None
) -> None:
    """A structured relationship reference must point at exactly one real
    other person (spec section 15). This does not police narrative text --
    it only constrains the explicit `related_person_id` field: it must be a
    well-formed person id, must not be the record's own id, and must
    resolve to an existing (active or archived) person. Legitimate
    relationship facts remain fully expressible; only dangling or
    free-form references are rejected, before any write happens."""
    if related_person_id is None:
        return
    if related_person_id == owner_person_id:
        raise MontaukValidationError(
            f"related_person_id {related_person_id!r} is the record's own person; a record "
            "is about exactly one person"
        )
    if not PERSON_ID_RE.match(related_person_id):
        raise MontaukValidationError(
            f"related_person_id {related_person_id!r} is not a valid person id (pass an "
            "existing person_id, not a name)"
        )
    if not (ctx.store.exists(related_person_id) or ctx.store.is_archived(related_person_id)):
        raise MontaukValidationError(
            f"related_person_id {related_person_id!r} does not refer to an existing person; "
            "search for or create that person first, or omit the reference"
        )


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
        if ctx.semantic_index is not None:
            ctx.semantic_index.upsert_person(person)
        return "ok"
    except Exception:
        logger.exception("failed to update derived index for person %r", person.id)
        return "degraded"


def _authorize_write(ctx: MontaukContext, mcp_ctx: Context) -> None:
    """No-op when auth isn't wired at all; otherwise resolves the calling
    agent's identity for this transport and requires read_write."""
    if ctx.credential_store is None:
        return
    headers = mcp_ctx.headers
    identity = resolve_http_identity(ctx.credential_store, headers) if headers is not None else ctx.stdio_identity
    require_write(identity)


def _replace_field(person: Person, **updates: object) -> Person:
    """Build a new, freshly-validated Person with the given fields replaced.
    Round-trips through model_dump()/the constructor rather than
    model_copy(), so field-level and cross-field validators (e.g. the
    duplicate-fact-id check) re-run on the result."""
    try:
        return Person(**{**person.model_dump(), **updates})
    except PydanticValidationError as exc:
        raise MontaukValidationError(str(exc)) from exc


def _apply_batch_op(
    person: Person,
    operation: BatchOperation,
    *,
    seen_fact_ids: set[str],
    seen_interaction_ids: set[str],
) -> tuple[Person, list[str]]:
    """Apply one batch operation to `person`, returning the new Person and
    the local IDs it changed. Raises immediately (NotFoundError /
    MontaukValidationError) on any invalid operation, before any write
    happens -- update_person_batch relies on this to validate the whole
    batch before touching disk.

    `seen_fact_ids`/`seen_interaction_ids` track every local ID ever
    allocated in this batch (seeded from the person's state before the
    batch started) and only ever grow, even across a remove_fact within
    the same batch -- otherwise a remove followed by an add later in the
    same batch could reissue the just-freed ID, which ids.py's
    monotonic-allocation invariant explicitly rules out.
    """
    if isinstance(operation, AddFactOp):
        fact_id = next_fact_id(seen_fact_ids)
        seen_fact_ids.add(fact_id)
        try:
            new_fact = Fact(
                id=fact_id,
                category=operation.category,
                text=operation.text,
                date=operation.date,
                confidence=operation.confidence,
                related_person_id=operation.related_person_id,
                sources=operation.sources,
            )
        except PydanticValidationError as exc:
            raise MontaukValidationError(str(exc)) from exc
        return _replace_field(person, facts=[*person.facts, new_fact]), [fact_id]

    if isinstance(operation, UpdateFactOp):
        existing = person.get_fact(operation.fact_id)
        if existing is None:
            raise NotFoundError(f"fact {operation.fact_id!r} not found on person {person.id!r}")
        field_updates = {k: v for k, v in operation.model_dump(exclude={"op", "fact_id"}).items() if v is not None}
        try:
            new_fact = Fact(**{**existing.model_dump(), **field_updates})
        except PydanticValidationError as exc:
            raise MontaukValidationError(str(exc)) from exc
        new_facts = [new_fact if f.id == operation.fact_id else f for f in person.facts]
        return _replace_field(person, facts=new_facts), [operation.fact_id]

    if isinstance(operation, RemoveFactOp):
        if person.get_fact(operation.fact_id) is None:
            raise NotFoundError(f"fact {operation.fact_id!r} not found on person {person.id!r}")
        remaining = [f for f in person.facts if f.id != operation.fact_id]
        return _replace_field(person, facts=remaining), [operation.fact_id]

    if isinstance(operation, RecordInteractionOp):
        interaction_id = next_interaction_id(seen_interaction_ids)
        seen_interaction_ids.add(interaction_id)
        try:
            new_interaction = Interaction(
                id=interaction_id,
                date=operation.date,
                channel=operation.channel,
                connection_level=operation.connection_level,
                summary=operation.summary,
                sources=operation.sources,
            )
        except PydanticValidationError as exc:
            raise MontaukValidationError(str(exc)) from exc
        return _replace_field(person, interactions=[*person.interactions, new_interaction]), [interaction_id]

    if isinstance(operation, UpdateContactDetailsOp):
        contact_updates = {k: v for k, v in operation.model_dump(exclude={"op"}).items() if v is not None}
        try:
            new_contact = ContactInfo(**{**person.contact.model_dump(), **contact_updates})
        except PydanticValidationError as exc:
            raise MontaukValidationError(str(exc)) from exc
        return _replace_field(person, contact=new_contact), []

    if isinstance(operation, UpdateSummaryOp):
        return _replace_field(person, summary=operation.summary), []

    if isinstance(operation, SetBirthdayOp):
        return _replace_field(person, birthday=operation.birthday), []

    if isinstance(operation, SetContactCadenceOp):
        return _replace_field(
            person, desired_contact_cadence_days=operation.desired_contact_cadence_days
        ), []

    raise AssertionError(f"unhandled batch operation type: {operation!r}")  # pragma: no cover


def register_batch_tools(server: MCPServer, ctx: MontaukContext) -> None:
    store, write_queue = ctx.store, ctx.write_queue

    @server.tool(
        description=(
            "Atomically apply multiple related changes to exactly one known person_id in a single "
            "call: any mix of add_fact, update_fact, remove_fact, record_interaction, "
            "update_contact_details, update_summary, set_birthday, and set_contact_cadence "
            "operations. The entire batch is validated before any change is written -- if any "
            "operation is invalid, none of them are applied. Prefer this over separate tool calls "
            "when one interaction or source event produces several related updates for one person. "
            f"{RECORD_SCOPING_RULE} {BATCH_SEPARATION_RULE}"
        )
    )
    async def update_person_batch(
        person_id: str, operations: list[BatchOperation], mcp_ctx: Context
    ) -> WriteResult:
        _authorize_write(ctx, mcp_ctx)

        def op_fn() -> WriteResult:
            person = _load_person_or_error(store, person_id)
            for operation in operations:
                if isinstance(operation, (AddFactOp, UpdateFactOp)):
                    _validate_related_person_id(ctx, person_id, operation.related_person_id)
            working = person
            seen_fact_ids = set(person.fact_ids())
            seen_interaction_ids = set(person.interaction_ids())
            changed_ids: list[str] = []
            for operation in operations:
                working, ids = _apply_batch_op(
                    working, operation, seen_fact_ids=seen_fact_ids, seen_interaction_ids=seen_interaction_ids
                )
                changed_ids.extend(ids)
            status = _write_and_index(ctx, working)
            return WriteResult(person_id=person_id, changed_ids=changed_ids, index_update_status=status)

        return await write_queue.submit(op_fn)


def register_core_tools(server: MCPServer, ctx: MontaukContext) -> None:
    store, sqlite_index, write_queue = ctx.store, ctx.sqlite_index, ctx.write_queue

    @server.tool(
        description=(
            "Search active relationship memory using a name, alias, employer, location, event, date, "
            "or other remembered detail -- including vague identifying descriptions, not just exact "
            "names (e.g. 'the robotics guy I met at an MIT mixer about a year ago'). Returns ranked "
            "candidates with match_evidence explaining why each matched. Multiple candidates are "
            "expected when identity is ambiguous -- do not guess; ask the user to clarify rather than "
            "picking one."
        )
    )
    async def search_people(query: str) -> SearchResult:
        needle = query.strip().lower()
        if not needle:
            return SearchResult(candidates=[])
        candidates: dict[str, PersonCandidate] = {}
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
                candidates[row["person_id"]] = PersonCandidate(
                    person_id=row["person_id"], name=row["name"], summary=summary, match_evidence=evidence
                )

        if ctx.semantic_index is not None:
            matches = ctx.semantic_index.search(
                query, limit=ctx.max_candidates, similarity_threshold=ctx.similarity_threshold
            )
            for match in matches:
                row = sqlite_index.get_row(match.person_id)
                if row is None:
                    continue  # stale chunk for an archived/removed person; the next write reconciles it
                snippet = match.text if len(match.text) <= 140 else f"{match.text[:137]}..."
                evidence_line = f"semantic match ({match.chunk_type}, score {match.score:.2f}): {snippet!r}"
                if match.person_id in candidates:
                    candidates[match.person_id].match_evidence.append(evidence_line)
                else:
                    candidates[match.person_id] = PersonCandidate(
                        person_id=match.person_id,
                        name=row["name"],
                        summary=row["summary"],
                        match_evidence=[evidence_line],
                    )

        return SearchResult(candidates=list(candidates.values())[: ctx.max_candidates])

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
            "this person. The initial summary must describe only this person -- do not fold in "
            "details about other people who happened to appear in the same source."
        )
    )
    async def create_person(
        name: str,
        mcp_ctx: Context,
        aliases: list[str] | None = None,
        birthday: str | None = None,
        location: str | None = None,
        company: str | None = None,
        job_title: str | None = None,
        desired_contact_cadence_days: int | None = None,
        summary: str | None = None,
    ) -> WriteResult:
        _authorize_write(ctx, mcp_ctx)

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
            "related_person_id only when this fact concerns another existing person by ID. "
            f"{RECORD_SCOPING_RULE}"
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
        sources: list[Source] | None = None,
    ) -> WriteResult:
        _authorize_write(ctx, mcp_ctx)

        def op() -> WriteResult:
            person = _load_person_or_error(store, person_id)
            _validate_related_person_id(ctx, person_id, related_person_id)
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
            "empty -- use remove_fact and add_fact for that). "
            f"{RECORD_SCOPING_RULE}"
        )
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
        _authorize_write(ctx, mcp_ctx)

        def op() -> WriteResult:
            person = _load_person_or_error(store, person_id)
            existing = person.get_fact(fact_id)
            if existing is None:
                raise NotFoundError(f"fact {fact_id!r} not found on person {person_id!r}")
            _validate_related_person_id(ctx, person_id, related_person_id)
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
    async def remove_fact(person_id: str, fact_id: str, mcp_ctx: Context) -> WriteResult:
        _authorize_write(ctx, mcp_ctx)

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
            "regardless. Do not pass a raw transcript or message thread; summarize concisely. "
            f"{INTERACTION_SCOPING_RULE}"
        )
    )
    async def record_interaction(
        person_id: str,
        date: str,
        mcp_ctx: Context,
        channel: str | None = None,
        connection_level: int | None = None,
        summary: str | None = None,
        sources: list[Source] | None = None,
    ) -> WriteResult:
        _authorize_write(ctx, mcp_ctx)

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
        mcp_ctx: Context,
        emails: list[str] | None = None,
        phones: list[str] | None = None,
        address: str | None = None,
        messaging: dict[str, str] | None = None,
    ) -> WriteResult:
        _authorize_write(ctx, mcp_ctx)

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

    @server.tool(
        description=(
            "Replace the short identifying summary for a known person_id. "
            f"{RECORD_SCOPING_RULE}"
        )
    )
    async def update_summary(person_id: str, summary: str, mcp_ctx: Context) -> WriteResult:
        _authorize_write(ctx, mcp_ctx)

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
    async def set_birthday(person_id: str, birthday: str | None, mcp_ctx: Context) -> WriteResult:
        _authorize_write(ctx, mcp_ctx)

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
    async def set_contact_cadence(
        person_id: str, desired_contact_cadence_days: int | None, mcp_ctx: Context
    ) -> WriteResult:
        _authorize_write(ctx, mcp_ctx)

        def op() -> WriteResult:
            person = _load_person_or_error(store, person_id)
            updated = _replace_field(person, desired_contact_cadence_days=desired_contact_cadence_days)
            status = _write_and_index(ctx, updated)
            return WriteResult(person_id=person_id, index_update_status=status)

        return await write_queue.submit(op)
