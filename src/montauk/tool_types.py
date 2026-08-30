"""MCP tool-facing request/response shapes, distinct from the pure domain
models in models.py. Kept compact per Appendix A: mutation tools return
person_id, changed object IDs, and index-update status; search tools
return concise evidence, not entire records.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

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


class SetBirthdayOp(BaseModel):
    op: Literal["set_birthday"] = "set_birthday"
    birthday: str | None = None


class SetContactCadenceOp(BaseModel):
    op: Literal["set_contact_cadence"] = "set_contact_cadence"
    desired_contact_cadence_days: int | None = None


BatchOperation = Annotated[
    Union[
        AddFactOp,
        UpdateFactOp,
        RemoveFactOp,
        RecordInteractionOp,
        UpdateContactDetailsOp,
        UpdateSummaryOp,
        SetBirthdayOp,
        SetContactCadenceOp,
    ],
    Field(discriminator="op"),
]
