"""Typed domain models for people, facts, interactions, and provenance
(spec sections 8, 9, 10, 12, 13).
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .dates import Birthday, FlexDate
from .ids import PERSON_ID_RE
from .schema import CATEGORIES, Confidence

_FACT_ID_RE = re.compile(r"^fact-\d+$")
_INTERACTION_ID_RE = re.compile(r"^int-\d+$")


def _blank_to_none(v: object) -> object:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    return v


class Source(BaseModel):
    """Compact provenance reference: type and ID only (spec section 13)."""

    model_config = ConfigDict(extra="forbid")

    type: str
    id: str

    @field_validator("type", "id")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("source type/id must not be empty")
        return v


class ContactInfo(BaseModel):
    """Current contact methods only (spec section 9); history belongs in facts."""

    model_config = ConfigDict(extra="forbid")

    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    address: str | None = None
    messaging: dict[str, str] = Field(default_factory=dict)


class Fact(BaseModel):
    """A single narrative fact within one fixed category (spec section 10)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    category: str
    date: FlexDate | None = None
    confidence: Confidence = Confidence.HIGH
    text: str
    sources: list[Source] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        if not _FACT_ID_RE.match(v):
            raise ValueError(f"fact id {v!r} must match 'fact-<N>'")
        return v

    @field_validator("category")
    @classmethod
    def _validate_category(cls, v: str) -> str:
        if v not in CATEGORIES:
            raise ValueError(f"category {v!r} is not one of the fixed categories: {CATEGORIES}")
        return v

    @field_validator("text")
    @classmethod
    def _non_empty_text(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("fact text must not be empty")
        return v

    @field_validator("date", mode="before")
    @classmethod
    def _blank_date(cls, v: object) -> object:
        return _blank_to_none(v)


class Interaction(BaseModel):
    """A recorded interaction, even one that produced no new facts (spec section 12)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    date: FlexDate
    channel: str | None = None
    connection_level: int | None = None
    summary: str | None = None
    sources: list[Source] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        if not _INTERACTION_ID_RE.match(v):
            raise ValueError(f"interaction id {v!r} must match 'int-<N>'")
        return v

    @field_validator("connection_level")
    @classmethod
    def _validate_connection_level(cls, v: int | None) -> int | None:
        if v is not None and not (1 <= v <= 6):
            raise ValueError(f"connection_level {v} must be between 1 and 6")
        return v


class Person(BaseModel):
    """A person record: structured fields plus categorized facts and
    interactions (spec sections 8, 9).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    birthday: Birthday | None = None
    location: str | None = None
    company: str | None = None
    job_title: str | None = None
    desired_contact_cadence_days: int | None = None
    summary: str | None = None
    contact: ContactInfo = Field(default_factory=ContactInfo)
    facts: list[Fact] = Field(default_factory=list)
    interactions: list[Interaction] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        if not PERSON_ID_RE.match(v):
            raise ValueError(f"person id {v!r} must be lowercase, alphanumeric, hyphen-separated")
        return v

    @field_validator("name")
    @classmethod
    def _non_empty_name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("person name must not be empty")
        return v

    @field_validator("desired_contact_cadence_days")
    @classmethod
    def _validate_cadence(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError(f"desired_contact_cadence_days {v} must be a positive integer or null")
        return v

    def fact_ids(self) -> list[str]:
        return [f.id for f in self.facts]

    def interaction_ids(self) -> list[str]:
        return [i.id for i in self.interactions]

    def get_fact(self, fact_id: str) -> Fact | None:
        return next((f for f in self.facts if f.id == fact_id), None)

    def get_interaction(self, interaction_id: str) -> Interaction | None:
        return next((i for i in self.interactions if i.id == interaction_id), None)

    def last_interaction_date(self) -> FlexDate | None:
        if not self.interactions:
            return None
        return max((i.date for i in self.interactions), key=lambda d: d.latest())
