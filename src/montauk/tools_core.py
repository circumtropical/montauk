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

from .auth import AgentIdentity, CredentialStore, require_write, resolve_http_identity
from .config import RetrievalConfig
from .errors import ArchivedError, MontaukValidationError, NotFoundError
from .ids import PERSON_ID_RE, next_fact_id, next_interaction_id, normalize_alias
from .markdown_store import MarkdownStore, PersonNotFoundError, person_to_markdown
from .models import ContactInfo, Fact, Interaction, Person, Source
from .person_context import DETAIL_LEVELS, build_person_context
from .schema import CATEGORIES
from .semantic_index import SemanticIndex
from .sqlite_index import SqliteIndex, compute_content_hash
from .tool_types import (
    AddFactOp,
    BatchOperation,
    IndexUpdateStatus,
    InteractionMutationResult,
    NameUpdateResult,
    PersonCandidate,
    PersonContextResponse,
    PersonCore,
    RecordInteractionOp,
    RemoveFactOp,
    SearchResult,
    SetBirthdayOp,
    SetContactCadenceOp,
    SetNameOp,
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

# Identity vs. name guidance (amendment: generic person IDs + mutable names).
NAME_UPDATE_RULE = (
    "Changes the existing person's display name in place. person_id (their permanent "
    "identity) and their canonical file are unchanged -- this is not archive-and-recreate. "
    "The previous name is kept as an alias by default; pass retain_previous_as_alias=false "
    "when the old value is wrong or should not stay associated with the person. Names are "
    "not unique: a matching name on another record does not make them the same person, and "
    "the change is never rejected or auto-merged for that reason."
)
INTERACTION_CORRECTION_RULE = (
    "Correcting inaccurate data is not erasing history. Use update_interaction when the "
    "interaction happened but a recorded detail is wrong (including the wrong participant -- "
    "pass move_to_person_id to re-attribute it, leaving no trace on the former person). Use "
    "remove_interaction only when the interaction record itself is erroneous, never "
    "occurred, or duplicates another. Never remove an accurate interaction just because it "
    "is old, inconvenient, sensitive, or no longer relevant."
)


def _find_possible_duplicates(
    ctx: MontaukContext, name: str, *, exclude_person_id: str | None = None
) -> list[PersonCandidate]:
    """Active people whose current display name or an alias matches `name`
    under normal name-normalization. Advisory only: Montauk never merges
    people or refuses a write because a name is shared (spec section 22)."""
    target = normalize_alias(name)
    matches: list[PersonCandidate] = []
    for row in ctx.sqlite_index.list_all():
        if row["person_id"] == exclude_person_id:
            continue
        names = [row["name"], *json.loads(row["aliases_json"])]
        if any(normalize_alias(n) == target for n in names):
            matches.append(
                PersonCandidate(
                    person_id=row["person_id"],
                    name=row["name"],
                    summary=row["summary"],
                    match_evidence=[f"existing record already uses the name {name!r}"],
                )
            )
    return matches


def apply_name_update(
    person: Person,
    *,
    name: str,
    retain_previous_as_alias: bool = True,
    aliases_to_add: list[str] | None = None,
    aliases_to_remove: list[str] | None = None,
) -> Person:
    """Single source of truth for a display-name change (used by both the
    update_person_name tool and the set_name batch op). Returns a new,
    freshly validated Person with the same id/file; alias normalization
    and current-name exclusion are handled by the Person model."""
    remove_keys = {normalize_alias(a) for a in (aliases_to_remove or [])}
    new_aliases = [a for a in person.aliases if normalize_alias(a) not in remove_keys]
    if retain_previous_as_alias and normalize_alias(person.name) != normalize_alias(name):
        new_aliases.append(person.name)
    new_aliases.extend(aliases_to_add or [])
    return _replace_field(person, name=name, aliases=new_aliases)


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
    # Purpose-specific retrieval budgets/toggles (prepare_person_context).
    # None -> library defaults (RetrievalConfig()).
    retrieval: RetrievalConfig | None = None
    # Set at startup when the whole semantic index is unusable as *current*
    # semantic evidence (model/schema/chunking drift, dimension mismatch)
    # and an automatic rebuild wasn't possible. Retrieval then falls back
    # to lexical and says so; it never serves known-stale semantic results.
    semantic_stale_reason: str | None = None

    @property
    def retrieval_config(self) -> RetrievalConfig:
        return self.retrieval or RetrievalConfig()


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
        content_hash = compute_content_hash(path)
        ctx.sqlite_index.upsert_person(person, file_path=str(path), content_hash=content_hash)
        if ctx.semantic_index is not None:
            ctx.semantic_index.upsert_person(person, content_hash=content_hash)
        return "ok"
    except Exception:
        logger.exception("failed to update derived index for person %r", person.id)
        return "degraded"


def _write_and_index_many(ctx: MontaukContext, *people: Person) -> IndexUpdateStatus:
    """Write and re-index several people in one serialized op (used by a
    participant correction, which moves one interaction between two
    records). Canonical Markdown for every person is written first; any
    derived-index failure downgrades the whole result to degraded."""
    status: IndexUpdateStatus = "ok"
    for person in people:
        if _write_and_index(ctx, person) == "degraded":
            status = "degraded"
    return status


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

    if isinstance(operation, SetNameOp):
        return apply_name_update(
            person,
            name=operation.name,
            retain_previous_as_alias=operation.retain_previous_as_alias,
            aliases_to_add=operation.aliases_to_add,
            aliases_to_remove=operation.aliases_to_remove,
        ), []

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
            "update_contact_details, update_summary, set_name, set_birthday, and set_contact_cadence "
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
            "fact and interaction. Use intentionally and only for explicit review, export, or "
            "maintenance -- for an ordinary question about a person, use prepare_person_context, "
            "which returns just the relevant evidence within a token budget. This returns "
            "substantially more content and consumes more model context."
        )
    )
    async def get_full_record(person_id: str) -> str:
        return person_to_markdown(_load_person_or_error(store, person_id))

    @server.tool(
        description=(
            "Return a compact, purpose-specific evidence packet about one known person_id: the "
            "facts, relationships, and interactions from their stored memory that are relevant to "
            "`purpose`, selected with hybrid lexical + semantic retrieval and trimmed to a token "
            "budget. Supply `purpose` as the actual question or task -- retrieval quality depends on "
            "it. Results are selected canonical memory, substantially verbatim; storage metadata is "
            "intentionally stripped. This is not Montauk's advice and not a generated answer: the "
            "calling agent interprets the evidence and is responsible for advice, recommendations, "
            "compatibility judgments, and any external research. If a specific detail is absent, the "
            "packet simply will not contain it -- do not invent names, quotations, or facts to fill "
            "the gap; narrow the purpose or fetch the cited records instead. detail_level: 'brief' "
            "(~identity/quick reminder), 'standard' (most questions), 'comprehensive' (organized "
            "briefing of all materially relevant content that fits). max_tokens optionally overrides "
            "the preset within server bounds. When retrieval.truncated is true or semantic_available "
            "is false, narrow the purpose or retrieve specific records with get_facts / "
            "get_interactions / get_full_record."
        )
    )
    async def prepare_person_context(
        person_id: str,
        purpose: str,
        detail_level: str = "standard",
        max_tokens: int | None = None,
    ) -> PersonContextResponse:
        if not purpose or not purpose.strip():
            raise MontaukValidationError("purpose must not be blank")
        if detail_level not in DETAIL_LEVELS:
            raise MontaukValidationError(
                f"detail_level must be one of {DETAIL_LEVELS}, got {detail_level!r}"
            )
        rcfg = ctx.retrieval_config
        if max_tokens is not None:
            if not (rcfg.min_tokens <= max_tokens <= rcfg.max_tokens):
                raise MontaukValidationError(
                    f"max_tokens must be within [{rcfg.min_tokens}, {rcfg.max_tokens}]"
                )
            budget = max_tokens
        else:
            budget = rcfg.budget_for(detail_level)

        person = _load_person_or_error(store, person_id)

        semantic_index = ctx.semantic_index
        stale_reason = ctx.semantic_stale_reason
        if semantic_index is not None and stale_reason is None:
            try:
                on_disk = compute_content_hash(store.person_path(person.id))
                indexed = semantic_index.person_content_hash(person.id)
                if indexed is not None and indexed != on_disk:
                    stale_reason = (
                        "semantic index for this person is behind the canonical record; "
                        "lexical results only until reindexed"
                    )
            except OSError:
                pass

        result = build_person_context(
            person,
            purpose,
            detail_level=detail_level,
            budget_tokens=budget,
            semantic_index=semantic_index if rcfg.semantic_enabled else None,
            semantic_stale_reason=stale_reason,
            lexical_enabled=rcfg.lexical_enabled,
        )
        return PersonContextResponse.model_validate(result.to_payload())

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
            "Create a new person record. Montauk assigns the permanent person_id (a generic "
            "identifier such as P0001); you supply the best name currently known, which may be "
            "partial, approximate, or a single word. Search first (search_people) if the person "
            "might already exist -- do not use this tool to resolve identity uncertainty; if search "
            "returns plausible candidates, ask the user to clarify instead of creating a duplicate. "
            "A shared name is allowed and never blocks creation: any existing people with the same "
            "name are returned in possible_duplicates for you to disambiguate, not merged. The "
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
            person_id = store.allocate_person_id()
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
            duplicates = _find_possible_duplicates(ctx, name)
            status = _write_and_index(ctx, person)
            return WriteResult(
                person_id=person.id, index_update_status=status, possible_duplicates=duplicates
            )

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
            "Correct a recorded interaction on person_id by interaction_id. Use this when the "
            "interaction happened but a detail is wrong. Only the fields you pass change; omitted "
            "fields keep their current value (like update_fact, this cannot clear a field to empty). "
            "The interaction_id never changes. To fix a wrong participant, pass move_to_person_id: "
            "the interaction is moved to that person (a fresh interaction_id is allocated there) and "
            "removed entirely from the original person -- no voided record or searchable trace is "
            "left behind. This is a correction, not deletion of history. " + INTERACTION_SCOPING_RULE
        )
    )
    async def update_interaction(
        person_id: str,
        interaction_id: str,
        mcp_ctx: Context,
        date: str | None = None,
        channel: str | None = None,
        connection_level: int | None = None,
        summary: str | None = None,
        sources: list[Source] | None = None,
        move_to_person_id: str | None = None,
        correction_reason: str | None = None,
    ) -> InteractionMutationResult:
        _authorize_write(ctx, mcp_ctx)

        def op() -> InteractionMutationResult:
            person = _load_person_or_error(store, person_id)
            existing = person.get_interaction(interaction_id)
            if existing is None:
                raise NotFoundError(
                    f"interaction {interaction_id!r} not found on person {person_id!r}"
                )
            overrides = {
                k: v
                for k, v in {
                    "date": date,
                    "channel": channel,
                    "connection_level": connection_level,
                    "summary": summary,
                    "sources": sources,
                }.items()
                if v is not None
            }
            merged = {**existing.model_dump(), **overrides}

            moving = move_to_person_id is not None and move_to_person_id != person_id
            if moving:
                target = _load_person_or_error(store, move_to_person_id)
                new_interaction_id = next_interaction_id(target.interaction_ids())
                merged["id"] = new_interaction_id
                try:
                    new_interaction = Interaction(**merged)
                except PydanticValidationError as exc:
                    raise MontaukValidationError(str(exc)) from exc
                source_updated = _replace_field(
                    person, interactions=[i for i in person.interactions if i.id != interaction_id]
                )
                target_updated = _replace_field(
                    target, interactions=[*target.interactions, new_interaction]
                )
                status = _write_and_index_many(ctx, source_updated, target_updated)
                logger.info(
                    "interaction %s re-attributed from person %s to person %s (as %s)",
                    interaction_id,
                    person_id,
                    move_to_person_id,
                    new_interaction_id,
                )
                return InteractionMutationResult(
                    person_id=person_id,
                    interaction_id=interaction_id,
                    operation="reattributed",
                    moved_to_person_id=move_to_person_id,
                    new_interaction_id=new_interaction_id,
                    affected_person_ids=[person_id, move_to_person_id],
                    index_update_status=status,
                )

            merged["id"] = interaction_id
            try:
                new_interaction = Interaction(**merged)
            except PydanticValidationError as exc:
                raise MontaukValidationError(str(exc)) from exc
            updated = _replace_field(
                person,
                interactions=[new_interaction if i.id == interaction_id else i for i in person.interactions],
            )
            status = _write_and_index(ctx, updated)
            logger.info("interaction %s/%s corrected", person_id, interaction_id)
            return InteractionMutationResult(
                person_id=person_id,
                interaction_id=interaction_id,
                operation="updated",
                affected_person_ids=[person_id],
                index_update_status=status,
            )

        return await write_queue.submit(op)

    @server.tool(
        description=(
            "Remove an interaction from person_id by interaction_id. Use this ONLY when the "
            "interaction record itself is erroneous: it never happened, it was created by mistake, "
            "or it duplicates another interaction. A non-empty correction_reason is required. The "
            "interaction and every reference to it are removed from active memory and indexes with "
            "no voided copy retained. Do NOT use this to remove an accurate interaction merely "
            "because it is old, inconvenient, sensitive, embarrassing, or no longer relevant -- "
            "accurate history is kept. To fix a wrong participant or detail, use update_interaction "
            "instead."
        )
    )
    async def remove_interaction(
        person_id: str, interaction_id: str, correction_reason: str, mcp_ctx: Context
    ) -> InteractionMutationResult:
        _authorize_write(ctx, mcp_ctx)
        if not correction_reason or not correction_reason.strip():
            raise MontaukValidationError("remove_interaction requires a non-empty correction_reason")

        def op() -> InteractionMutationResult:
            person = _load_person_or_error(store, person_id)
            if person.get_interaction(interaction_id) is None:
                raise NotFoundError(
                    f"interaction {interaction_id!r} not found on person {person_id!r}"
                )
            updated = _replace_field(
                person, interactions=[i for i in person.interactions if i.id != interaction_id]
            )
            status = _write_and_index(ctx, updated)
            logger.info("interaction %s/%s removed as erroneous", person_id, interaction_id)
            return InteractionMutationResult(
                person_id=person_id,
                interaction_id=interaction_id,
                operation="removed",
                affected_person_ids=[person_id],
                index_update_status=status,
            )

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
            "Change the display name of an existing person_id. " + NAME_UPDATE_RULE + " Use this "
            "(not archive + create_person) to fix a misspelled, partial, or outdated name. "
            "aliases_to_add / aliases_to_remove are applied atomically with the rename; aliases are "
            "de-duplicated and the current name is never stored as its own alias."
        )
    )
    async def update_person_name(
        person_id: str,
        name: str,
        mcp_ctx: Context,
        retain_previous_as_alias: bool = True,
        aliases_to_add: list[str] | None = None,
        aliases_to_remove: list[str] | None = None,
    ) -> NameUpdateResult:
        _authorize_write(ctx, mcp_ctx)

        def op() -> NameUpdateResult:
            person = _load_person_or_error(store, person_id)
            try:
                updated = apply_name_update(
                    person,
                    name=name,
                    retain_previous_as_alias=retain_previous_as_alias,
                    aliases_to_add=aliases_to_add,
                    aliases_to_remove=aliases_to_remove,
                )
            except PydanticValidationError as exc:
                raise MontaukValidationError(str(exc)) from exc
            duplicates = _find_possible_duplicates(ctx, name, exclude_person_id=person_id)
            status = _write_and_index(ctx, updated)
            return NameUpdateResult(
                person_id=person.id,
                name=updated.name,
                aliases=updated.aliases,
                index_update_status=status,
                possible_duplicates=duplicates,
            )

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
