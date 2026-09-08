"""PostgreSQL canonical schema (spec section 8).

Every tenant-owned row carries ``workspace_id`` directly (spec section 7).
Unique constraints include ``workspace_id`` wherever a value is only unique
within a workspace. Repositories (``db/repositories.py``) are the only code
that builds queries against these tables, and they never expose an
unscoped list/get.

In scope for the database/migration/dashboard increment: identity/auth,
relationship memory, settings, revisions, audit, and the legacy migration
log. The source archive (spec 8.3) and extraction/review tables
(``review_proposals``, ``model_configurations``, ``summary_cache``,
``backup_runs``) are added by the connector/extraction increment; they
attach by ``workspace_id`` / ``person_id`` exactly like these tables, so
their absence forces no rewrite (ADR 0001).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin, utcnow, uuid_pk

# --- enumerated string values (kept as CHECK constraints, not PG enums, so
#     adding a value later is a plain migration, not an ALTER TYPE dance) ---

FACT_CATEGORIES = (
    "Family",
    "Work & Education",
    "Interests",
    "Relationship with User",
    "Life Events",
    "General Notes",
)
CONFIDENCE_VALUES = ("high", "medium", "low")
DATE_PRECISION_VALUES = ("day", "month", "year")
AUTHORITY_VALUES = ("owner_curated", "agent_curated", "automatically_extracted")
HUMAN_ROLES = ("owner", "admin", "editor", "viewer")
ACTOR_TYPES = ("owner", "user", "agent", "system", "migration")


# --- identity and authorization (spec 8.1) ------------------------------


class Workspace(Base, TimestampMixin):
    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = uuid_pk()
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    # Deployment context captured by the first-run wizard (spec 25.1).
    public_url: Mapped[str | None] = mapped_column(String(500))
    deployment_profile: Mapped[str | None] = mapped_column(String(32))

    memberships: Mapped[list[WorkspaceMembership]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )
    settings: Mapped[WorkspaceSettings | None] = relationship(
        back_populates="workspace", cascade="all, delete-orphan", uselist=False
    )


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(String(320), unique=True)
    # Argon2id (argon2-cffi). Never the raw password.
    password_hash: Mapped[str] = mapped_column(Text)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    memberships: Mapped[list[WorkspaceMembership]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class WorkspaceMembership(Base, TimestampMixin):
    __tablename__ = "workspace_memberships"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id"),
        CheckConstraint("role IN " + str(HUMAN_ROLES), name="role_valid"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16))

    workspace: Mapped[Workspace] = relationship(back_populates="memberships")
    user: Mapped[User] = relationship(back_populates="memberships")


class Session(Base):
    """Dashboard login session. The cookie carries an opaque random token;
    only its SHA-256 hash is stored (spec 25.1, 27)."""

    __tablename__ = "sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    csrf_secret: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentCredential(Base, TimestampMixin):
    """Manual token fallback for agent access (spec 22.2). OAuth-style
    dynamic client registration is a later increment."""

    __tablename__ = "agent_credentials"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    token_prefix: Mapped[str] = mapped_column(String(16))
    # Named, revocable capabilities (spec 21). Stored as a JSON array of strings.
    capabilities: Mapped[list[str]] = mapped_column(JSONB, default=list)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


# --- relationship memory (spec 8.2) ------------------------------------


class Person(Base, TimestampMixin):
    __tablename__ = "people"
    __table_args__ = (
        UniqueConstraint("workspace_id", "public_id"),
        Index("ix_people_workspace_name", "workspace_id", "name"),
        Index("ix_people_workspace_birthday", "workspace_id", "birthday_month", "birthday_day"),
        CheckConstraint(
            "connection_level IS NULL OR (connection_level BETWEEN 1 AND 6)",
            name="connection_level_range",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    # Permanent public identifier: P[0-9]{4,}, unique within workspace,
    # never reused (spec 8.5).
    public_id: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(Text)

    birthday_month: Mapped[int | None] = mapped_column(Integer)
    birthday_day: Mapped[int | None] = mapped_column(Integer)
    birthday_year: Mapped[int | None] = mapped_column(Integer)
    location: Mapped[str | None] = mapped_column(Text)
    company: Mapped[str | None] = mapped_column(Text)
    job_title: Mapped[str | None] = mapped_column(Text)
    desired_contact_cadence_days: Mapped[int | None] = mapped_column(Integer)
    summary: Mapped[str | None] = mapped_column(Text)
    connection_level: Mapped[int | None] = mapped_column(Integer)

    archived_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    aliases: Mapped[list[PersonAlias]] = relationship(back_populates="person", cascade="all, delete-orphan")
    contact_methods: Mapped[list[PersonContactMethod]] = relationship(
        back_populates="person", cascade="all, delete-orphan"
    )
    facts: Mapped[list[Fact]] = relationship(
        back_populates="person",
        cascade="all, delete-orphan",
        foreign_keys="Fact.person_id",
    )
    interactions: Mapped[list[Interaction]] = relationship(
        back_populates="person", cascade="all, delete-orphan"
    )


class PersonAlias(Base):
    __tablename__ = "person_aliases"
    __table_args__ = (UniqueConstraint("person_id", "alias_normalized"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    person_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), index=True)
    # Display spelling preserved as given; normalized form for comparison
    # only (spec 8.2 "Normalized for comparison while preserving display").
    alias: Mapped[str] = mapped_column(Text)
    alias_normalized: Mapped[str] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, default=0)

    person: Mapped[Person] = relationship(back_populates="aliases")


class PersonContactMethod(Base):
    __tablename__ = "person_contact_methods"
    __table_args__ = (
        CheckConstraint("kind IN ('email', 'phone', 'address', 'messaging')", name="kind_valid"),
        Index("ix_person_contact_methods_lookup", "workspace_id", "kind", "value_normalized"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    person_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    # For messaging methods, the platform key (e.g. "signal"); NULL otherwise.
    label: Mapped[str | None] = mapped_column(String(64))
    value: Mapped[str] = mapped_column(Text)
    value_normalized: Mapped[str] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, default=0)

    person: Mapped[Person] = relationship(back_populates="contact_methods")


class Fact(Base, TimestampMixin):
    __tablename__ = "facts"
    __table_args__ = (
        UniqueConstraint("person_id", "local_id"),
        CheckConstraint("category IN " + str(FACT_CATEGORIES), name="category_valid"),
        CheckConstraint("confidence IN " + str(CONFIDENCE_VALUES), name="confidence_valid"),
        CheckConstraint("authority IN " + str(AUTHORITY_VALUES), name="authority_valid"),
        CheckConstraint(
            "date_precision IS NULL OR date_precision IN " + str(DATE_PRECISION_VALUES),
            name="date_precision_valid",
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    person_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), index=True)
    # Person-local human-facing id, "fact-N" (spec 8.5). Preserved across migration.
    local_id: Mapped[str] = mapped_column(String(32))
    category: Mapped[str] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(Text)
    date_text: Mapped[str | None] = mapped_column(String(16))
    date_precision: Mapped[str | None] = mapped_column(String(8))
    confidence: Mapped[str] = mapped_column(String(8), default="high")
    authority: Mapped[str] = mapped_column(String(24), default="owner_curated")
    # One-way reference to another person (spec 15); never mirrored. Kept as
    # the person's UUID with the public id denormalized for export/display.
    related_person_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("people.id", ondelete="SET NULL"))
    related_person_public_id: Mapped[str | None] = mapped_column(String(16))

    person: Mapped[Person] = relationship(back_populates="facts", foreign_keys=[person_id])
    sources: Mapped[list[FactSource]] = relationship(back_populates="fact", cascade="all, delete-orphan")


class FactSource(Base):
    __tablename__ = "fact_sources"

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    fact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("facts.id", ondelete="CASCADE"), index=True)
    # Phase 1 Source: a compact (type, id) provenance reference (spec 13).
    source_type: Mapped[str] = mapped_column(String(64))
    source_ref: Mapped[str] = mapped_column(Text)

    fact: Mapped[Fact] = relationship(back_populates="sources")


class Relationship(Base, TimestampMixin):
    """Explicit relationship object (spec 8.2). Not populated by the Phase 1
    migration -- Phase 1 relationship data is carried losslessly on
    ``facts.related_person_id`` to preserve exact round-trip semantics
    (ADR 0001). Present so the extraction increment can add explicit
    relationships without a schema change."""

    __tablename__ = "relationships"

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    person_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), index=True)
    related_person_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"))
    kind: Mapped[str | None] = mapped_column(String(64))
    text: Mapped[str | None] = mapped_column(Text)
    authority: Mapped[str] = mapped_column(String(24), default="owner_curated")


class Interaction(Base, TimestampMixin):
    __tablename__ = "interactions"
    __table_args__ = (
        UniqueConstraint("person_id", "local_id"),
        CheckConstraint("authority IN " + str(AUTHORITY_VALUES), name="authority_valid"),
        CheckConstraint("date_precision IN " + str(DATE_PRECISION_VALUES), name="date_precision_valid"),
        CheckConstraint(
            "connection_level IS NULL OR (connection_level BETWEEN 1 AND 6)",
            name="connection_level_range",
        ),
        Index("ix_interactions_person_latest", "person_id", "occurred_on_latest"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    person_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), index=True)
    local_id: Mapped[str] = mapped_column(String(32))
    date_text: Mapped[str] = mapped_column(String(16))
    date_precision: Mapped[str] = mapped_column(String(8))
    # Latest calendar date consistent with the recorded precision, for
    # cadence/recency queries (mirrors Phase 1 last_interaction_at). Not
    # canonical -- derived from date_text on every write.
    occurred_on_latest: Mapped[dt.date] = mapped_column()
    channel: Mapped[str | None] = mapped_column(String(64))
    connection_level: Mapped[int | None] = mapped_column(Integer)
    summary: Mapped[str | None] = mapped_column(Text)
    authority: Mapped[str] = mapped_column(String(24), default="owner_curated")

    person: Mapped[Person] = relationship(back_populates="interactions")
    sources: Mapped[list[InteractionSource]] = relationship(
        back_populates="interaction", cascade="all, delete-orphan"
    )


class InteractionSource(Base):
    __tablename__ = "interaction_sources"

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    interaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("interactions.id", ondelete="CASCADE"), index=True
    )
    source_type: Mapped[str] = mapped_column(String(64))
    source_ref: Mapped[str] = mapped_column(Text)

    interaction: Mapped[Interaction] = relationship(back_populates="sources")


class RecordRevision(Base):
    """Append-only, field-level revision history (spec 20.2). Never updated
    or deleted; restore writes a new revision."""

    __tablename__ = "record_revisions"
    __table_args__ = (
        Index("ix_record_revisions_entity", "workspace_id", "entity_type", "entity_id"),
        Index("ix_record_revisions_person", "workspace_id", "person_id", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[uuid.UUID] = mapped_column()
    # The person this change is "about", for the person-page history view.
    person_id: Mapped[uuid.UUID | None] = mapped_column()
    field: Mapped[str] = mapped_column(String(64))
    old_value: Mapped[Any | None] = mapped_column(JSONB)
    new_value: Mapped[Any | None] = mapped_column(JSONB)
    actor_type: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[str | None] = mapped_column(String(200))
    authority: Mapped[str | None] = mapped_column(String(24))
    reason: Mapped[str | None] = mapped_column(Text)
    source_run_id: Mapped[uuid.UUID | None] = mapped_column()
    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --- settings and operations (spec 8.4, partial) ----------------------


class WorkspaceSettings(Base, TimestampMixin):
    __tablename__ = "workspace_settings"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    # Appendix B defaults. Extraction fields are inert until the extraction
    # increment wires a provider, but stored now so the schema is stable.
    review_threshold: Mapped[str] = mapped_column(String(24), default="automatic_all")
    allowed_reviewers: Mapped[str] = mapped_column(String(32), default="human_only")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    daily_extraction_time: Mapped[str] = mapped_column(String(5), default="03:00")
    historical_ingestion_default: Mapped[str] = mapped_column(String(24), default="all_history")
    agent_transcript_access: Mapped[bool] = mapped_column(default=False)

    # LLM cost controls (spec 16.3). NULL = no limit.
    monthly_spend_limit_usd: Mapped[float | None] = mapped_column(Numeric(10, 2))
    monthly_token_limit: Mapped[int | None] = mapped_column(Integer)
    max_job_input_tokens: Mapped[int] = mapped_column(Integer, default=60_000)
    # Owner pause switch, independent of the limit hard-stop (spec 16.3).
    llm_processing_paused: Mapped[bool] = mapped_column(default=False)

    workspace: Mapped[Workspace] = relationship(back_populates="settings")


class PersonIdSequence(Base):
    """Per-workspace monotonic high-water mark for public person ids
    (spec 8.5). Advanced transactionally; never reused, never lowered."""

    __tablename__ = "person_id_sequences"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    last_allocated: Mapped[int] = mapped_column(Integer, default=0)


class AuditEvent(Base):
    """Operational audit log (spec 28): IDs, counts, status, timing. Never
    message/fact content."""

    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_ws_time", "workspace_id", "occurred_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    actor_type: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[str | None] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(64))
    resource_type: Mapped[str | None] = mapped_column(String(48))
    resource_id: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default="ok")
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[Any | None] = mapped_column(JSONB)
    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ModelConfiguration(Base, TimestampMixin):
    """Per-workspace, per-purpose LLM configuration (spec 8.4, 16.1). Two
    purposes -- ``summarization`` and ``extraction`` -- can point at
    different providers/models. A row exists only after an explicit owner
    action; its absence (or ``provider_type='none'``) means no LLM."""

    __tablename__ = "model_configurations"
    __table_args__ = (
        UniqueConstraint("workspace_id", "purpose"),
        CheckConstraint("purpose IN ('summarization', 'extraction')", name="purpose_valid"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    purpose: Mapped[str] = mapped_column(String(16))
    provider_type: Mapped[str] = mapped_column(String(24), default="none")
    model: Mapped[str] = mapped_column(String(120), default="")
    base_url: Mapped[str | None] = mapped_column(String(500))
    cli_binary: Mapped[str | None] = mapped_column(String(120))
    # API key: AES-GCM ciphertext via SecretBox; never stored in plaintext.
    api_key_ciphertext: Mapped[str | None] = mapped_column(Text)
    # Optional owner-supplied pricing metadata for cost estimates (spec 16.3).
    price_input_per_mtok: Mapped[float | None] = mapped_column(Numeric(10, 4))
    price_output_per_mtok: Mapped[float | None] = mapped_column(Numeric(10, 4))
    extra_config: Mapped[Any | None] = mapped_column(JSONB)
    enabled: Mapped[bool] = mapped_column(default=True)


class LLMUsageEvent(Base):
    """One provider call's accounting (spec 16.3, 18): counts, cost, model,
    outcome. Never message/fact content."""

    __tablename__ = "llm_usage_events"
    __table_args__ = (Index("ix_llm_usage_ws_time", "workspace_id", "occurred_at"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    purpose: Mapped[str] = mapped_column(String(16))
    provider_type: Mapped[str] = mapped_column(String(24))
    model: Mapped[str] = mapped_column(String(120))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6))
    reported_cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6))
    ok: Mapped[bool] = mapped_column(default=True)
    error_category: Mapped[str | None] = mapped_column(String(32))
    run_ref: Mapped[str | None] = mapped_column(String(200))
    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SummaryCacheEntry(Base):
    """A generated purpose-specific summary (spec 24.2): a cache entry, not
    an authoritative fact. Invalidated when the person's memory changes."""

    __tablename__ = "summary_cache"
    __table_args__ = (
        Index("ix_summary_cache_person", "workspace_id", "person_id"),
        UniqueConstraint("workspace_id", "person_id", "cache_key"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    person_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), index=True)
    # sha256 over (normalized purpose, detail_level) -- one cached summary per
    # purpose per person.
    cache_key: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(Text)
    detail_level: Mapped[str] = mapped_column(String(16))
    generated: Mapped[bool] = mapped_column(default=False)
    body: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(120))
    provider_type: Mapped[str | None] = mapped_column(String(24))
    schema_version: Mapped[str] = mapped_column(String(16), default="v1")
    evidence_refs: Mapped[Any | None] = mapped_column(JSONB)
    # Hash of the curated memory the summary was built from; a mismatch on
    # read means the person's record changed -> stale.
    memory_fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    invalidated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class LegacyMigrationRun(Base):
    """One Phase 1 -> Phase 2 migration attempt (spec 30). The report is the
    machine-readable verification artifact (spec 30.5)."""

    __tablename__ = "legacy_migration_runs"

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workspaces.id", ondelete="SET NULL"))
    workspace_slug: Mapped[str] = mapped_column(String(64))
    source_path: Mapped[str] = mapped_column(Text)
    source_manifest_hash: Mapped[str | None] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(16))  # dry_run | execute
    status: Mapped[str] = mapped_column(String(24), default="in_progress")
    backup_ref: Mapped[str | None] = mapped_column(Text)
    report: Mapped[Any | None] = mapped_column(JSONB)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
