"""MCP tool-facing request/response shapes, distinct from the pure domain
models in models.py. Kept compact per Appendix A: mutation tools return
person_id, changed object IDs, and index-update status; search tools
return concise evidence, not entire records.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from .models import ContactInfo, Source

IndexUpdateStatus = Literal["ok", "degraded"]


class PersonCore(BaseModel):
    """Structured core fields and short summary (spec section 21) -- no
    facts or interactions, to keep targeted reads context-efficient."""

    id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    birthday: str | None = None
    location: str | None = None
    company: str | None = None
    job_title: str | None = None
    desired_contact_cadence_days: int | None = None
    summary: str | None = None
    contact: ContactInfo = Field(default_factory=ContactInfo)


class PersonCandidate(BaseModel):
    person_id: str
    name: str
    summary: str | None = None
    match_evidence: list[str] = Field(default_factory=list)


class SearchResult(BaseModel):
    candidates: list[PersonCandidate] = Field(default_factory=list)


class WriteResult(BaseModel):
    person_id: str
    changed_ids: list[str] = Field(default_factory=list)
    status: Literal["ok"] = "ok"
    index_update_status: IndexUpdateStatus = "ok"
    # Names are not unique identifiers: create_person and update_person_name
    # surface any other existing people whose current name/alias matches,
    # for the agent to disambiguate. Montauk never auto-merges them.
    possible_duplicates: list[PersonCandidate] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class NameUpdateResult(BaseModel):
    """Result of update_person_name: identity is unchanged (`person_id`),
    only the display name and aliases moved."""

    person_id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    index_update_status: IndexUpdateStatus = "ok"
    possible_duplicates: list[PersonCandidate] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class InteractionMutationResult(BaseModel):
    """Result of update_interaction / remove_interaction."""

    person_id: str
    interaction_id: str
    operation: Literal["updated", "reattributed", "removed"]
    # For a participant correction, the person the interaction now belongs
    # to (and its freshly allocated interaction id there).
    moved_to_person_id: str | None = None
    new_interaction_id: str | None = None
    affected_person_ids: list[str] = Field(default_factory=list)
    index_update_status: IndexUpdateStatus = "ok"


# --- update_person_batch operations (spec section 23) ---------------------
#
# One discriminated-union member per mutation kind supported inside a batch.
# The `op` field is the discriminator, giving each operation a fully
# explicit schema (per spec section 25.3's "schemas should be explicit
# enough that invalid calls are rejected before handler logic").


class AddFactOp(BaseModel):
    op: Literal["add_fact"] = "add_fact"
    category: str
    text: str
    date: str | None = None
    confidence: str = "high"
    related_person_id: str | None = None
    sources: list[Source] = Field(default_factory=list)


class UpdateFactOp(BaseModel):
    op: Literal["update_fact"] = "update_fact"
    fact_id: str
    text: str | None = None
    category: str | None = None
    date: str | None = None
    confidence: str | None = None
    related_person_id: str | None = None


class RemoveFactOp(BaseModel):
    op: Literal["remove_fact"] = "remove_fact"
    fact_id: str


class RecordInteractionOp(BaseModel):
    op: Literal["record_interaction"] = "record_interaction"
    date: str
    channel: str | None = None
    connection_level: int | None = None
    summary: str | None = None
    sources: list[Source] = Field(default_factory=list)


class UpdateContactDetailsOp(BaseModel):
    op: Literal["update_contact_details"] = "update_contact_details"
    emails: list[str] | None = None
    phones: list[str] | None = None
    address: str | None = None
    messaging: dict[str, str] | None = None


class UpdateSummaryOp(BaseModel):
    op: Literal["update_summary"] = "update_summary"
    summary: str


class SetNameOp(BaseModel):
    op: Literal["set_name"] = "set_name"
    name: str
    retain_previous_as_alias: bool = True
    aliases_to_add: list[str] = Field(default_factory=list)
    aliases_to_remove: list[str] = Field(default_factory=list)


class SetBirthdayOp(BaseModel):
    op: Literal["set_birthday"] = "set_birthday"
    birthday: str | None = None


class SetContactCadenceOp(BaseModel):
    op: Literal["set_contact_cadence"] = "set_contact_cadence"
    desired_contact_cadence_days: int | None = None


class UpcomingBirthday(BaseModel):
    person_id: str
    name: str
    birthday: str
    next_occurrence: str
    days_until: int


class OverdueContact(BaseModel):
    person_id: str
    name: str
    desired_contact_cadence_days: int
    last_interaction_at: str | None = None
    days_since_last_interaction: int | None = None
    status: Literal["overdue", "never_contacted"]


class ArchiveResult(BaseModel):
    person_id: str
    status: Literal["archived", "restored"] = "archived"


class ArchivedPersonSummary(BaseModel):
    person_id: str
    name: str


class ValidationIssueOut(BaseModel):
    person_id: str | None
    file_path: str
    error_type: str
    severity: Literal["error", "warning"]
    message: str


class ValidationReportSummary(BaseModel):
    scanned_at: str
    healthy: bool
    valid_person_count: int
    error_count: int
    warning_count: int
    issues: list[ValidationIssueOut] = Field(default_factory=list)


class HealthStatus(BaseModel):
    healthy: bool
    valid_person_count: int
    error_count: int
    warning_count: int
    last_scanned_at: str | None = None
    last_reconciliation_at: str | None = None


BatchOperation = Annotated[
    (
        AddFactOp
        | UpdateFactOp
        | RemoveFactOp
        | RecordInteractionOp
        | UpdateContactDetailsOp
        | UpdateSummaryOp
        | SetNameOp
        | SetBirthdayOp
        | SetContactCadenceOp
    ),
    Field(discriminator="op"),
]
