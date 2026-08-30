"""MCP tool-facing request/response shapes, distinct from the pure domain
models in models.py. Kept compact per Appendix A: mutation tools return
person_id, changed object IDs, and index-update status; search tools
return concise evidence, not entire records.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .models import ContactInfo

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
