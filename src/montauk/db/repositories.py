"""Workspace-scoped repositories (spec 7).

Every method takes its workspace from the :class:`WorkspaceScope` it was
constructed with and filters by ``workspace_id`` in the same query. There
is no unscoped ``list``/``get`` -- a caller cannot accidentally read across
workspaces, and cross-workspace ids simply resolve to ``None``.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, selectinload

from ..ids import PERSON_ID_RE, next_fact_id, next_interaction_id, normalize_alias
from ..models import Fact as DomainFact
from ..models import Interaction as DomainInteraction
from ..models import Person as DomainPerson
from ..models import Source
from . import ids as id_alloc
from . import mapping
from . import models as orm
from .base import utcnow


class LocalRecordNotFound(LookupError):
    pass


class RelatedPersonInvalid(ValueError):
    pass


class PersonNotFound(LookupError):
    def __init__(self, public_id: str):
        self.public_id = public_id
        super().__init__(f"person {public_id!r} not found in this workspace")


@dataclass(frozen=True)
class Actor:
    """Who is making a change, for revision/audit attribution."""

    type: str  # owner | user | agent | system | migration
    id: str | None = None

    @classmethod
    def system(cls) -> Actor:
        return cls("system")

    @classmethod
    def migration(cls, run_id: str) -> Actor:
        return cls("migration", run_id)


@dataclass
class WorkspaceScope:
    session: Session
    workspace_id: uuid.UUID
    actor: Actor = field(default_factory=Actor.system)


# --- people ----------------------------------------------------------


_PERSON_LOADERS = (
    selectinload(orm.Person.aliases),
    selectinload(orm.Person.contact_methods),
    selectinload(orm.Person.facts).selectinload(orm.Fact.sources),
    selectinload(orm.Person.interactions).selectinload(orm.Interaction.sources),
)


class PeopleRepository:
    def __init__(self, scope: WorkspaceScope):
        self.scope = scope
        self.session = scope.session
        self.workspace_id = scope.workspace_id

    # -- reads --

    def _base(self) -> Select[tuple[orm.Person]]:
        return (
            select(orm.Person).where(orm.Person.workspace_id == self.workspace_id).options(*_PERSON_LOADERS)
        )

    def get(self, public_id: str, *, include_archived: bool = True) -> orm.Person | None:
        stmt = self._base().where(orm.Person.public_id == public_id)
        if not include_archived:
            stmt = stmt.where(orm.Person.archived_at.is_(None))
        return self.session.execute(stmt).scalar_one_or_none()

    def get_by_uuid(self, person_uuid: uuid.UUID) -> orm.Person | None:
        return self.session.execute(self._base().where(orm.Person.id == person_uuid)).scalar_one_or_none()

    def require(self, public_id: str, *, include_archived: bool = True) -> orm.Person:
        row = self.get(public_id, include_archived=include_archived)
        if row is None:
            raise PersonNotFound(public_id)
        return row

    def exists(self, public_id: str) -> bool:
        return (
            self.session.execute(
                select(orm.Person.id)
                .where(orm.Person.workspace_id == self.workspace_id)
                .where(orm.Person.public_id == public_id)
            ).first()
            is not None
        )

    def list_people(
        self,
        *,
        archived: bool | None = False,
        query: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        order_by: str = "name",
    ) -> list[orm.Person]:
        stmt = self._base()
        if archived is True:
            stmt = stmt.where(orm.Person.archived_at.is_not(None))
        elif archived is False:
            stmt = stmt.where(orm.Person.archived_at.is_(None))
        if query:
            stmt = self._apply_text_filter(stmt, query)
        stmt = stmt.order_by(
            orm.Person.public_id if order_by == "public_id" else func.lower(orm.Person.name),
            orm.Person.public_id,
        )
        if offset:
            stmt = stmt.offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.execute(stmt).scalars().unique())

    def _apply_text_filter(self, stmt: Select[tuple[orm.Person]], query: str) -> Select[tuple[orm.Person]]:
        like = f"%{query.strip().lower()}%"
        alias_exists = (
            select(orm.PersonAlias.id)
            .where(orm.PersonAlias.person_id == orm.Person.id)
            .where(func.lower(orm.PersonAlias.alias).like(like))
        )
        contact_exists = (
            select(orm.PersonContactMethod.id)
            .where(orm.PersonContactMethod.person_id == orm.Person.id)
            .where(func.lower(orm.PersonContactMethod.value).like(like))
        )
        return stmt.where(
            func.lower(orm.Person.name).like(like)
            | func.coalesce(func.lower(orm.Person.company), "").like(like)
            | func.coalesce(func.lower(orm.Person.location), "").like(like)
            | func.coalesce(func.lower(orm.Person.summary), "").like(like)
            | alias_exists.exists()
            | contact_exists.exists()
        )

    def count(self, *, archived: bool | None = False) -> int:
        stmt = select(func.count(orm.Person.id)).where(orm.Person.workspace_id == self.workspace_id)
        if archived is True:
            stmt = stmt.where(orm.Person.archived_at.is_not(None))
        elif archived is False:
            stmt = stmt.where(orm.Person.archived_at.is_(None))
        return int(self.session.execute(stmt).scalar_one())

    def find_by_name(self, name: str, *, exclude_public_id: str | None = None) -> list[orm.Person]:
        """Active people whose current name or an alias matches under
        name-normalization. Advisory only -- never merges (spec 22)."""
        key = normalize_alias(name)
        out: list[orm.Person] = []
        for row in self.list_people(archived=False):
            if row.public_id == exclude_public_id:
                continue
            names = [row.name, *(a.alias for a in row.aliases)]
            if any(normalize_alias(n) == key for n in names):
                out.append(row)
        return out

    def resolve_public_ids(self, public_ids: set[str]) -> dict[str, uuid.UUID]:
        if not public_ids:
            return {}
        rows = self.session.execute(
            select(orm.Person.public_id, orm.Person.id)
            .where(orm.Person.workspace_id == self.workspace_id)
            .where(orm.Person.public_id.in_(public_ids))
        ).all()
        return {pid: uid for pid, uid in rows}

    # -- writes --

    def allocate_public_id(self) -> str:
        return id_alloc.allocate_public_id(self.session, self.workspace_id)

    def create(
        self,
        domain: DomainPerson,
        *,
        authority: str = "owner_curated",
        archived: bool = False,
        record_revision: bool = True,
    ) -> orm.Person:
        """Insert a person and all children from a validated domain model.
        ``domain.id`` is used as the public id verbatim -- the caller
        allocates it (dashboard create) or preserves it (migration)."""
        row = orm.Person(
            workspace_id=self.workspace_id,
            public_id=domain.id,
            name=domain.name,
            birthday_month=domain.birthday.month if domain.birthday else None,
            birthday_day=domain.birthday.day if domain.birthday else None,
            birthday_year=domain.birthday.year if domain.birthday else None,
            location=domain.location,
            company=domain.company,
            job_title=domain.job_title,
            desired_contact_cadence_days=domain.desired_contact_cadence_days,
            summary=domain.summary,
            archived_at=utcnow() if archived else None,
        )
        self.session.add(row)
        self.session.flush()
        self._rebuild_children(row, domain, authority=authority, related_map=None)
        if record_revision:
            self._log(
                entity_type="person",
                entity_id=row.id,
                person_id=row.id,
                field="__created__",
                old=None,
                new={"public_id": row.public_id, "name": row.name},
                authority=authority,
            )
        return row

    def link_related_person_ids(self, people: list[orm.Person]) -> None:
        """Second migration pass: resolve every ``related_person_public_id``
        on already-inserted facts to a real ``related_person_id`` UUID within
        the workspace. A reference that doesn't resolve stays dangling (its
        public id is kept for the report; spec 30.4 step 9)."""
        wanted: set[str] = {
            f.related_person_public_id for p in people for f in p.facts if f.related_person_public_id
        }
        resolved = self.resolve_public_ids(wanted)
        for person in people:
            for fact in person.facts:
                if fact.related_person_public_id:
                    fact.related_person_id = resolved.get(fact.related_person_public_id)

    def set_archived(self, row: orm.Person, archived: bool, *, reason: str | None = None) -> None:
        was = row.archived_at is not None
        if was == archived:
            return
        row.archived_at = utcnow() if archived else None
        self._log(
            entity_type="person",
            entity_id=row.id,
            person_id=row.id,
            field="archived_at",
            old=was,
            new=archived,
            reason=reason,
        )

    def replace(
        self,
        row: orm.Person,
        domain: DomainPerson,
        *,
        authority: str = "owner_curated",
        reason: str | None = None,
    ) -> None:
        """Rewrite a person from a validated domain model, recording a
        field-level revision for every changed scalar, fact, and interaction
        (spec 20.2). Structural rebuild of children keeps local ids stable."""
        before = mapping.person_to_domain(row)
        self._diff_scalars(row, before, domain, authority=authority, reason=reason)

        row.name = domain.name
        row.birthday_month = domain.birthday.month if domain.birthday else None
        row.birthday_day = domain.birthday.day if domain.birthday else None
        row.birthday_year = domain.birthday.year if domain.birthday else None
        row.location = domain.location
        row.company = domain.company
        row.job_title = domain.job_title
        row.desired_contact_cadence_days = domain.desired_contact_cadence_days
        row.summary = domain.summary

        row.aliases.clear()
        row.contact_methods.clear()
        row.facts.clear()
        row.interactions.clear()
        self.session.flush()
        self._rebuild_children(row, domain, authority=authority, related_map="resolve")

    def update_core_fields(
        self,
        row: orm.Person,
        domain: DomainPerson,
        *,
        reason: str | None = None,
    ) -> None:
        """Update only the structured fields (name, aliases, birthday,
        location, company, job_title, cadence, summary, contact methods)
        from a validated domain model. Facts and interactions on ``domain``
        are ignored -- their rows are left untouched. Field-level revisions
        are recorded for every change (spec 20.2). Owner-curated authority.

        Empty / omitted values are stored as NULL, so partial information is
        first-class: a location of just "Boston", a birthday of just "03".
        """
        before = mapping.person_to_domain(row)
        self._diff_scalars(row, before, domain, authority="owner_curated", reason=reason)

        row.name = domain.name
        row.birthday_month = domain.birthday.month if domain.birthday else None
        row.birthday_day = domain.birthday.day if domain.birthday else None
        row.birthday_year = domain.birthday.year if domain.birthday else None
        row.location = domain.location
        row.company = domain.company
        row.job_title = domain.job_title
        row.desired_contact_cadence_days = domain.desired_contact_cadence_days
        row.summary = domain.summary

        if before.contact.model_dump() != domain.contact.model_dump():
            self._log(
                entity_type="person",
                entity_id=row.id,
                person_id=row.id,
                field="contact",
                old=before.contact.model_dump(),
                new=domain.contact.model_dump(),
                authority="owner_curated",
                reason=reason,
            )

        row.aliases.clear()
        row.contact_methods.clear()
        self.session.flush()

        for pos, alias in enumerate(domain.aliases):
            row.aliases.append(
                orm.PersonAlias(
                    workspace_id=self.workspace_id,
                    alias=alias,
                    alias_normalized=normalize_alias(alias),
                    position=pos,
                )
            )
        for method in mapping.contact_methods_from_info(
            domain.contact, workspace_id=self.workspace_id, person_id=row.id
        ):
            row.contact_methods.append(method)
        self.session.flush()

    # -- facts --

    def _resolve_related(self, owner: orm.Person, related_public_id: str | None) -> uuid.UUID | None:
        """Validate a structured relationship reference: well-formed id, not
        the record's own, and resolves to a real person in this workspace
        (active or archived). Returns the UUID, or None if no reference."""
        if not related_public_id:
            return None
        rid = related_public_id.strip()
        if rid == owner.public_id:
            raise RelatedPersonInvalid("a record is about exactly one person; it cannot relate to itself")
        if not PERSON_ID_RE.match(rid):
            raise RelatedPersonInvalid(f"{rid!r} is not a valid person id (use a person's P-number)")
        resolved = self.resolve_public_ids({rid})
        if rid not in resolved:
            raise RelatedPersonInvalid(f"no person {rid!r} in this workspace -- create or find them first")
        return resolved[rid]

    def add_fact(
        self,
        row: orm.Person,
        *,
        category: str,
        text: str,
        date: str | None = None,
        confidence: str = "high",
        related_person_id: str | None = None,
        sources: list[Source] | None = None,
        reason: str | None = None,
        authority: str = "owner_curated",
    ) -> str:
        local_id = next_fact_id(f.local_id for f in row.facts)
        fact = DomainFact.model_validate(
            {
                "id": local_id,
                "category": category,
                "text": text,
                "date": date or None,
                "confidence": confidence,
                "related_person_id": (related_person_id.strip() or None) if related_person_id else None,
                "sources": sources or [],
            }
        )
        related_uuid = self._resolve_related(row, fact.related_person_id)
        date_text, precision = mapping.flexdate_columns(fact.date)
        frow = orm.Fact(
            workspace_id=self.workspace_id,
            local_id=local_id,
            category=fact.category,
            text=fact.text,
            date_text=date_text,
            date_precision=precision,
            confidence=fact.confidence.value,
            authority=authority,
            related_person_public_id=fact.related_person_id,
            related_person_id=related_uuid,
        )
        for s in fact.sources:
            frow.sources.append(
                orm.FactSource(workspace_id=self.workspace_id, source_type=s.type, source_ref=s.id)
            )
        row.facts.append(frow)
        row.updated_at = utcnow()
        self.session.flush()
        self._log(
            entity_type="fact",
            entity_id=frow.id,
            person_id=row.id,
            field="__created__",
            old=None,
            new={"category": fact.category, "text": fact.text},
            authority=authority,
            reason=reason,
        )
        return local_id

    def _get_fact(self, row: orm.Person, local_id: str) -> orm.Fact:
        for f in row.facts:
            if f.local_id == local_id:
                return f
        raise LocalRecordNotFound(f"fact {local_id!r} not on {row.public_id}")

    def update_fact(
        self,
        row: orm.Person,
        local_id: str,
        *,
        text: str | None = None,
        category: str | None = None,
        date: str | None = None,
        confidence: str | None = None,
        related_person_id: str | None = None,
        clear_related: bool = False,
        sources: list[Source] | None = None,
        reason: str | None = None,
    ) -> None:
        frow = self._get_fact(row, local_id)
        current = DomainFact.model_validate(
            {
                "id": local_id,
                "category": frow.category,
                "text": frow.text,
                "date": frow.date_text or None,
                "confidence": frow.confidence,
                "related_person_id": frow.related_person_public_id,
                "sources": [Source(type=s.source_type, id=s.source_ref) for s in frow.sources],
            }
        )
        new_related = current.related_person_id
        if clear_related:
            new_related = None
        elif related_person_id is not None and related_person_id.strip():
            new_related = related_person_id.strip()
        updated = DomainFact.model_validate(
            {
                "id": local_id,
                "category": category or current.category,
                "text": text if text is not None else current.text,
                "date": (date or None) if date is not None else current.date,
                "confidence": confidence or current.confidence.value,
                "related_person_id": new_related,
                "sources": sources if sources is not None else current.sources,
            }
        )
        related_uuid = self._resolve_related(row, updated.related_person_id)

        changes: dict[str, tuple[object, object]] = {}
        if updated.text != current.text:
            changes["text"] = (current.text, updated.text)
        if updated.category != current.category:
            changes["category"] = (current.category, updated.category)
        old_date = current.date.to_string() if current.date else None
        new_date = updated.date.to_string() if updated.date else None
        if old_date != new_date:
            changes["date"] = (old_date, new_date)
        if updated.confidence != current.confidence:
            changes["confidence"] = (current.confidence.value, updated.confidence.value)
        if updated.related_person_id != current.related_person_id:
            changes["related_person_id"] = (current.related_person_id, updated.related_person_id)

        date_text, precision = mapping.flexdate_columns(updated.date)
        frow.category = updated.category
        frow.text = updated.text
        frow.date_text = date_text
        frow.date_precision = precision
        frow.confidence = updated.confidence.value
        frow.related_person_public_id = updated.related_person_id
        frow.related_person_id = related_uuid
        if sources is not None:
            frow.sources.clear()
            self.session.flush()
            for s in sources:
                frow.sources.append(
                    orm.FactSource(workspace_id=self.workspace_id, source_type=s.type, source_ref=s.id)
                )
        # An owner edit lifts the fact to owner-curated authority.
        frow.authority = "owner_curated"
        row.updated_at = utcnow()
        self.session.flush()
        for fld, (old, new) in changes.items():
            self._log(
                entity_type="fact",
                entity_id=frow.id,
                person_id=row.id,
                field=fld,
                old=old,
                new=new,
                authority="owner_curated",
                reason=reason,
            )

    def remove_fact(self, row: orm.Person, local_id: str, *, reason: str | None = None) -> None:
        frow = self._get_fact(row, local_id)
        snapshot = {"category": frow.category, "text": frow.text}
        fact_uuid = frow.id
        row.facts.remove(frow)
        row.updated_at = utcnow()
        self.session.flush()
        self._log(
            entity_type="fact",
            entity_id=fact_uuid,
            person_id=row.id,
            field="__removed__",
            old=snapshot,
            new=None,
            authority="owner_curated",
            reason=reason,
        )

    # -- interactions --

    def _get_interaction(self, row: orm.Person, local_id: str) -> orm.Interaction:
        for i in row.interactions:
            if i.local_id == local_id:
                return i
        raise LocalRecordNotFound(f"interaction {local_id!r} not on {row.public_id}")

    def add_interaction(
        self,
        row: orm.Person,
        *,
        date: str,
        channel: str | None = None,
        connection_level: int | None = None,
        summary: str | None = None,
        sources: list[Source] | None = None,
        reason: str | None = None,
        authority: str = "owner_curated",
    ) -> str:
        local_id = next_interaction_id(i.local_id for i in row.interactions)
        interaction = DomainInteraction.model_validate(
            {
                "id": local_id,
                "date": date,
                "channel": channel or None,
                "connection_level": connection_level,
                "summary": summary or None,
                "sources": sources or [],
            }
        )
        date_text, precision = mapping.flexdate_columns_required(interaction.date)
        irow = orm.Interaction(
            workspace_id=self.workspace_id,
            local_id=local_id,
            date_text=date_text,
            date_precision=precision,
            occurred_on_latest=interaction.date.latest(),
            channel=interaction.channel,
            connection_level=interaction.connection_level,
            summary=interaction.summary,
            authority=authority,
        )
        for s in interaction.sources:
            irow.sources.append(
                orm.InteractionSource(workspace_id=self.workspace_id, source_type=s.type, source_ref=s.id)
            )
        row.interactions.append(irow)
        row.updated_at = utcnow()
        self.session.flush()
        self._log(
            entity_type="interaction",
            entity_id=irow.id,
            person_id=row.id,
            field="__created__",
            old=None,
            new={"date": interaction.date.to_string(), "summary": interaction.summary},
            authority=authority,
            reason=reason,
        )
        return local_id

    def update_interaction(
        self,
        row: orm.Person,
        local_id: str,
        *,
        date: str | None = None,
        channel: str | None = None,
        connection_level: int | None = None,
        clear_connection_level: bool = False,
        summary: str | None = None,
        sources: list[Source] | None = None,
        reason: str | None = None,
    ) -> None:
        irow = self._get_interaction(row, local_id)
        current = DomainInteraction.model_validate(
            {
                "id": local_id,
                "date": irow.date_text,
                "channel": irow.channel,
                "connection_level": irow.connection_level,
                "summary": irow.summary,
                "sources": [Source(type=s.source_type, id=s.source_ref) for s in irow.sources],
            }
        )
        new_level = (
            None
            if clear_connection_level
            else (connection_level if connection_level is not None else current.connection_level)
        )
        updated = DomainInteraction.model_validate(
            {
                "id": local_id,
                "date": date or current.date.to_string(),
                "channel": (channel if channel is not None else current.channel) or None,
                "connection_level": new_level,
                "summary": (summary if summary is not None else current.summary) or None,
                "sources": sources if sources is not None else current.sources,
            }
        )
        changes: dict[str, tuple[object, object]] = {}
        if updated.date.to_string() != current.date.to_string():
            changes["date"] = (current.date.to_string(), updated.date.to_string())
        if updated.channel != current.channel:
            changes["channel"] = (current.channel, updated.channel)
        if updated.connection_level != current.connection_level:
            changes["connection_level"] = (current.connection_level, updated.connection_level)
        if updated.summary != current.summary:
            changes["summary"] = (current.summary, updated.summary)

        date_text, precision = mapping.flexdate_columns_required(updated.date)
        irow.date_text = date_text
        irow.date_precision = precision
        irow.occurred_on_latest = updated.date.latest()
        irow.channel = updated.channel
        irow.connection_level = updated.connection_level
        irow.summary = updated.summary
        if sources is not None:
            irow.sources.clear()
            self.session.flush()
            for s in sources:
                irow.sources.append(
                    orm.InteractionSource(workspace_id=self.workspace_id, source_type=s.type, source_ref=s.id)
                )
        irow.authority = "owner_curated"
        row.updated_at = utcnow()
        self.session.flush()
        for fld, (old, new) in changes.items():
            self._log(
                entity_type="interaction",
                entity_id=irow.id,
                person_id=row.id,
                field=fld,
                old=old,
                new=new,
                authority="owner_curated",
                reason=reason,
            )

    def remove_interaction(self, row: orm.Person, local_id: str, *, reason: str | None = None) -> None:
        irow = self._get_interaction(row, local_id)
        snapshot = {"date": irow.date_text, "summary": irow.summary}
        interaction_uuid = irow.id
        row.interactions.remove(irow)
        row.updated_at = utcnow()
        self.session.flush()
        self._log(
            entity_type="interaction",
            entity_id=interaction_uuid,
            person_id=row.id,
            field="__removed__",
            old=snapshot,
            new=None,
            authority="owner_curated",
            reason=reason,
        )

    # -- internals --

    def _rebuild_children(
        self,
        row: orm.Person,
        domain: DomainPerson,
        *,
        authority: str,
        related_map: str | None,
    ) -> None:
        for pos, alias in enumerate(domain.aliases):
            row.aliases.append(
                orm.PersonAlias(
                    workspace_id=self.workspace_id,
                    alias=alias,
                    alias_normalized=normalize_alias(alias),
                    position=pos,
                )
            )
        for method in mapping.contact_methods_from_info(
            domain.contact, workspace_id=self.workspace_id, person_id=row.id
        ):
            row.contact_methods.append(method)

        resolved: dict[str, uuid.UUID] = {}
        if related_map == "resolve":
            resolved = self.resolve_public_ids(
                {f.related_person_id for f in domain.facts if f.related_person_id}
            )

        for fact in domain.facts:
            date_text, precision = mapping.flexdate_columns(fact.date)
            frow = orm.Fact(
                workspace_id=self.workspace_id,
                local_id=fact.id,
                category=fact.category,
                text=fact.text,
                date_text=date_text,
                date_precision=precision,
                confidence=fact.confidence.value,
                authority=authority,
                related_person_public_id=fact.related_person_id,
                related_person_id=resolved.get(fact.related_person_id) if fact.related_person_id else None,
            )
            for src in fact.sources:
                frow.sources.append(
                    orm.FactSource(workspace_id=self.workspace_id, source_type=src.type, source_ref=src.id)
                )
            row.facts.append(frow)

        for interaction in domain.interactions:
            date_text, precision = mapping.flexdate_columns(interaction.date)
            assert interaction.date is not None
            irow = orm.Interaction(
                workspace_id=self.workspace_id,
                local_id=interaction.id,
                date_text=date_text,
                date_precision=precision,
                occurred_on_latest=interaction.date.latest(),
                channel=interaction.channel,
                connection_level=interaction.connection_level,
                summary=interaction.summary,
                authority=authority,
            )
            for src in interaction.sources:
                irow.sources.append(
                    orm.InteractionSource(
                        workspace_id=self.workspace_id, source_type=src.type, source_ref=src.id
                    )
                )
            row.interactions.append(irow)
        self.session.flush()

    def _diff_scalars(
        self,
        row: orm.Person,
        before: DomainPerson,
        after: DomainPerson,
        *,
        authority: str,
        reason: str | None,
    ) -> None:
        fields = (
            "name",
            "location",
            "company",
            "job_title",
            "desired_contact_cadence_days",
            "summary",
        )
        for f in fields:
            old, new = getattr(before, f), getattr(after, f)
            if old != new:
                self._log(
                    entity_type="person",
                    entity_id=row.id,
                    person_id=row.id,
                    field=f,
                    old=old,
                    new=new,
                    authority=authority,
                    reason=reason,
                )
        if (before.birthday.to_string() if before.birthday else None) != (
            after.birthday.to_string() if after.birthday else None
        ):
            self._log(
                entity_type="person",
                entity_id=row.id,
                person_id=row.id,
                field="birthday",
                old=before.birthday.to_string() if before.birthday else None,
                new=after.birthday.to_string() if after.birthday else None,
                authority=authority,
                reason=reason,
            )
        if before.aliases != after.aliases:
            self._log(
                entity_type="person",
                entity_id=row.id,
                person_id=row.id,
                field="aliases",
                old=before.aliases,
                new=after.aliases,
                authority=authority,
                reason=reason,
            )

    def _log(
        self,
        *,
        entity_type: str,
        entity_id: uuid.UUID,
        person_id: uuid.UUID | None,
        field: str,
        old: object,
        new: object,
        authority: str | None = None,
        reason: str | None = None,
    ) -> None:
        self.session.add(
            orm.RecordRevision(
                workspace_id=self.workspace_id,
                entity_type=entity_type,
                entity_id=entity_id,
                person_id=person_id,
                field=field,
                old_value=old,
                new_value=new,
                actor_type=self.scope.actor.type,
                actor_id=self.scope.actor.id,
                authority=authority,
                reason=reason,
            )
        )


# --- revision history reads -----------------------------------------


class RevisionRepository:
    def __init__(self, scope: WorkspaceScope):
        self.session = scope.session
        self.workspace_id = scope.workspace_id

    def for_person(self, person_uuid: uuid.UUID, *, limit: int = 200) -> list[orm.RecordRevision]:
        return list(
            self.session.execute(
                select(orm.RecordRevision)
                .where(orm.RecordRevision.workspace_id == self.workspace_id)
                .where(orm.RecordRevision.person_id == person_uuid)
                .order_by(orm.RecordRevision.occurred_at.desc())
                .limit(limit)
            ).scalars()
        )


# --- birthday / cadence helpers (dashboard home + agent tools) -------


@dataclass(frozen=True)
class OverdueRow:
    public_id: str
    name: str
    cadence_days: int
    last_interaction_on: dt.date | None
    days_since: int | None


def upcoming_birthdays(
    scope: WorkspaceScope, *, within_days: int = 30, today: dt.date | None = None
) -> list[tuple[orm.Person, dt.date, int]]:
    from ..dates import Birthday

    today = today or dt.date.today()
    repo = PeopleRepository(scope)
    out: list[tuple[orm.Person, dt.date, int]] = []
    for person in repo.list_people(archived=False):
        if person.birthday_month is None or person.birthday_day is None:
            continue
        bday = Birthday(month=person.birthday_month, day=person.birthday_day, year=person.birthday_year)
        occ = bday.next_occurrence(today)
        days = (occ - today).days
        if days <= within_days:
            out.append((person, occ, days))
    out.sort(key=lambda t: t[2])
    return out


def overdue_contacts(scope: WorkspaceScope, *, today: dt.date | None = None) -> list[OverdueRow]:
    today = today or dt.date.today()
    session = scope.session
    latest = (
        select(
            orm.Interaction.person_id.label("pid"),
            func.max(orm.Interaction.occurred_on_latest).label("last_on"),
        )
        .where(orm.Interaction.workspace_id == scope.workspace_id)
        .group_by(orm.Interaction.person_id)
        .subquery()
    )
    rows = session.execute(
        select(orm.Person, latest.c.last_on)
        .where(orm.Person.workspace_id == scope.workspace_id)
        .where(orm.Person.archived_at.is_(None))
        .where(orm.Person.desired_contact_cadence_days.is_not(None))
        .outerjoin(latest, latest.c.pid == orm.Person.id)
    ).all()
    out: list[OverdueRow] = []
    for person, last_on in rows:
        cadence = person.desired_contact_cadence_days
        if last_on is None:
            out.append(OverdueRow(person.public_id, person.name, cadence, None, None))
            continue
        days_since = (today - last_on).days
        if days_since >= cadence:
            out.append(OverdueRow(person.public_id, person.name, cadence, last_on, days_since))
    out.sort(key=lambda r: (r.days_since is not None, -(r.days_since or 10**9)))
    return out


__all__ = [
    "Actor",
    "OverdueRow",
    "PeopleRepository",
    "PersonNotFound",
    "RevisionRepository",
    "WorkspaceScope",
    "overdue_contacts",
    "upcoming_birthdays",
]
