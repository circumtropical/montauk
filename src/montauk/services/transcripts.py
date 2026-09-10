"""Manual transcript imports: archive a WhatsApp text export idempotently
(spec 14, 15). Archiving never depends on an LLM -- extraction is a
separate step (``services/extraction.py``).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import models as orm
from ..db.repositories import PeopleRepository, WorkspaceScope
from ..ids import normalize_alias
from .whatsapp import PARSER_VERSION, parse_whatsapp_export

PLATFORM = "whatsapp_import"
_OWNER_NAMES = {"you", "me"}


class TranscriptError(ValueError):
    pass


@dataclass
class ImportResult:
    thread_id: uuid.UUID
    thread_title: str
    parsed_messages: int
    new_messages: int
    duplicate_messages: int
    participants: list[str]
    awaiting_processing: int
    warnings: list[str] = field(default_factory=list)
    already_imported_identical_file: bool = False


def _thread_key(participant_norms: list[str]) -> str:
    payload = "\x1f".join([PLATFORM, *sorted(set(participant_norms))])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _fingerprint(thread_key: str, sender_norm: str, sent_at: dt.datetime, text: str) -> str:
    payload = "\x1f".join([PLATFORM, thread_key, sender_norm, sent_at.isoformat(), " ".join(text.split())])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _guess_participant(repo: PeopleRepository, display_name: str, norm: str) -> tuple[str, uuid.UUID | None]:
    """First-pass guess for a newly-seen sender. WhatsApp labels the
    exporting user's own messages "You"; other names are matched to an
    existing person by name/alias when the match is unambiguous."""
    if norm in _OWNER_NAMES:
        return "owner", None
    matches = repo.find_by_name(display_name)
    if len(matches) == 1:
        return "person", matches[0].id
    return "unmapped", None


def _guess_owner_from_pair(participants: list[orm.SourceParticipant]) -> None:
    """In a two-person thread where one side is mapped to a person and the
    other is still unmapped, the unmapped side is almost certainly the owner."""
    if len(participants) != 2:
        return
    mapped = [p for p in participants if p.role == "person"]
    unmapped = [p for p in participants if p.role == "unmapped"]
    if len(mapped) == 1 and len(unmapped) == 1:
        unmapped[0].role = "owner"


def import_whatsapp(
    session: Session,
    scope: WorkspaceScope,
    *,
    filename: str,
    content: bytes,
    created_by: uuid.UUID | None,
) -> ImportResult:
    ws = scope.workspace_id
    parsed = parse_whatsapp_export(content)
    if not parsed.messages:
        raise TranscriptError(
            "no WhatsApp messages found in this file -- export a chat as a .txt (without media)"
        )
    if not parsed.participants:
        raise TranscriptError("this export has no identifiable participants")

    file_sha = hashlib.sha256(content).hexdigest()
    norms = [normalize_alias(p) for p in parsed.participants]
    thread_key = _thread_key(norms)

    thread = session.execute(
        select(orm.SourceThread)
        .where(orm.SourceThread.workspace_id == ws)
        .where(orm.SourceThread.platform == PLATFORM)
        .where(orm.SourceThread.thread_key == thread_key)
    ).scalar_one_or_none()
    title = " & ".join(parsed.participants[:4]) + ("  …" if len(parsed.participants) > 4 else "")
    if thread is None:
        thread = orm.SourceThread(workspace_id=ws, platform=PLATFORM, thread_key=thread_key, title=title)
        session.add(thread)
        session.flush()
    else:
        thread.title = title
        thread.updated_at = dt.datetime.now(dt.UTC)

    repo = PeopleRepository(scope)
    existing = {
        p.display_name_normalized: p
        for p in session.execute(
            select(orm.SourceParticipant).where(orm.SourceParticipant.thread_id == thread.id)
        ).scalars()
    }
    for name, norm in zip(parsed.participants, norms, strict=True):
        if norm in existing:
            continue
        role, person_id = _guess_participant(repo, name, norm)
        part = orm.SourceParticipant(
            workspace_id=ws,
            thread_id=thread.id,
            display_name=name,
            display_name_normalized=norm,
            role=role,
            person_id=person_id,
        )
        session.add(part)
        existing[norm] = part
    session.flush()
    _guess_owner_from_pair(list(existing.values()))
    session.flush()

    imp = orm.SourceImport(
        workspace_id=ws,
        thread_id=thread.id,
        filename=filename[:500],
        file_sha256=file_sha,
        parser_version=PARSER_VERSION,
        message_count=len(parsed.messages),
        warnings=parsed.warnings or None,
        created_by=created_by,
    )
    session.add(imp)
    session.flush()

    seen = {
        r
        for r in session.execute(
            select(orm.SourceMessage.fingerprint).where(orm.SourceMessage.thread_id == thread.id)
        ).scalars()
    }
    inserted = 0
    for m in parsed.messages:
        sender_norm = normalize_alias(m.sender) if m.sender else ""
        fp = _fingerprint(thread_key, sender_norm, m.sent_at, m.text)
        if fp in seen:
            continue
        seen.add(fp)
        session.add(
            orm.SourceMessage(
                workspace_id=ws,
                thread_id=thread.id,
                import_id=imp.id,
                fingerprint=fp,
                sender_name=m.sender,
                sender_normalized=sender_norm or None,
                sent_at=m.sent_at,
                text=m.text,
                is_system=m.is_system,
                media_omitted=m.media_omitted,
                processing_status="skipped" if (m.is_system or m.media_omitted) else "awaiting_processing",
            )
        )
        inserted += 1
    imp.new_message_count = inserted
    session.flush()

    awaiting = int(
        session.execute(
            select(func.count(orm.SourceMessage.id))
            .where(orm.SourceMessage.thread_id == thread.id)
            .where(orm.SourceMessage.processing_status == "awaiting_processing")
        ).scalar_one()
    )
    return ImportResult(
        thread_id=thread.id,
        thread_title=thread.title,
        parsed_messages=len(parsed.messages),
        new_messages=inserted,
        duplicate_messages=len(parsed.messages) - inserted,
        participants=parsed.participants,
        awaiting_processing=awaiting,
        warnings=parsed.warnings,
        already_imported_identical_file=inserted == 0,
    )


# --- reads / mapping -------------------------------------------------


@dataclass
class ParticipantView:
    id: uuid.UUID
    display_name: str
    role: str
    person_public_id: str | None
    person_name: str | None
    message_count: int


@dataclass
class ThreadView:
    id: uuid.UUID
    title: str
    platform: str
    participants: list[ParticipantView]
    total_messages: int
    awaiting_processing: int
    processed: int
    first_at: dt.datetime | None
    last_at: dt.datetime | None
    recent: list[orm.SourceMessage]
    imports: list[orm.SourceImport]

    @property
    def mapped_people(self) -> int:
        return sum(1 for p in self.participants if p.role == "person" and p.person_public_id)

    @property
    def has_owner(self) -> bool:
        return any(p.role == "owner" for p in self.participants)

    @property
    def ready_to_extract(self) -> bool:
        return self.awaiting_processing > 0 and self.mapped_people > 0


def _require_thread(session: Session, scope: WorkspaceScope, thread_id: uuid.UUID) -> orm.SourceThread:
    thread = session.execute(
        select(orm.SourceThread)
        .where(orm.SourceThread.workspace_id == scope.workspace_id)
        .where(orm.SourceThread.id == thread_id)
    ).scalar_one_or_none()
    if thread is None:
        raise TranscriptError("no such transcript in this workspace")
    return thread


def list_threads(session: Session, scope: WorkspaceScope) -> list[ThreadView]:
    threads = (
        session.execute(
            select(orm.SourceThread)
            .where(orm.SourceThread.workspace_id == scope.workspace_id)
            .order_by(orm.SourceThread.updated_at.desc())
        )
        .scalars()
        .all()
    )
    return [get_thread(session, scope, t.id, recent_limit=0) for t in threads]


def get_thread(
    session: Session, scope: WorkspaceScope, thread_id: uuid.UUID, *, recent_limit: int = 12
) -> ThreadView:
    thread = _require_thread(session, scope, thread_id)

    counts: dict[str | None, int] = {
        k: v
        for k, v in session.execute(
            select(orm.SourceMessage.sender_normalized, func.count(orm.SourceMessage.id))
            .where(orm.SourceMessage.thread_id == thread.id)
            .group_by(orm.SourceMessage.sender_normalized)
        ).all()
    }
    status_counts: dict[str, int] = {
        k: v
        for k, v in session.execute(
            select(orm.SourceMessage.processing_status, func.count(orm.SourceMessage.id))
            .where(orm.SourceMessage.thread_id == thread.id)
            .group_by(orm.SourceMessage.processing_status)
        ).all()
    }
    span = session.execute(
        select(func.min(orm.SourceMessage.sent_at), func.max(orm.SourceMessage.sent_at)).where(
            orm.SourceMessage.thread_id == thread.id
        )
    ).one()

    parts = (
        session.execute(
            select(orm.SourceParticipant)
            .where(orm.SourceParticipant.thread_id == thread.id)
            .order_by(orm.SourceParticipant.display_name)
        )
        .scalars()
        .all()
    )
    person_ids = {p.person_id for p in parts if p.person_id}
    people = (
        {p.id: p for p in session.execute(select(orm.Person).where(orm.Person.id.in_(person_ids))).scalars()}
        if person_ids
        else {}
    )
    participant_views = [
        ParticipantView(
            id=p.id,
            display_name=p.display_name,
            role=p.role,
            person_public_id=people[p.person_id].public_id if p.person_id in people else None,
            person_name=people[p.person_id].name if p.person_id in people else None,
            message_count=int(counts.get(p.display_name_normalized, 0)),
        )
        for p in parts
    ]

    recent: list[orm.SourceMessage] = []
    if recent_limit:
        recent = list(
            session.execute(
                select(orm.SourceMessage)
                .where(orm.SourceMessage.thread_id == thread.id)
                .order_by(orm.SourceMessage.sent_at.desc())
                .limit(recent_limit)
            ).scalars()
        )
        recent.reverse()
    imports = list(
        session.execute(
            select(orm.SourceImport)
            .where(orm.SourceImport.thread_id == thread.id)
            .order_by(orm.SourceImport.created_at.desc())
        ).scalars()
    )
    return ThreadView(
        id=thread.id,
        title=thread.title,
        platform=thread.platform,
        participants=participant_views,
        total_messages=sum(status_counts.values()),
        awaiting_processing=int(status_counts.get("awaiting_processing", 0)),
        processed=int(status_counts.get("processed", 0)),
        first_at=span[0],
        last_at=span[1],
        recent=recent,
        imports=imports,
    )


def map_participant(
    session: Session,
    scope: WorkspaceScope,
    thread_id: uuid.UUID,
    participant_id: uuid.UUID,
    *,
    role: str,
    person_public_id: str | None,
) -> None:
    if role not in orm.PARTICIPANT_ROLES:
        raise TranscriptError(f"role must be one of {orm.PARTICIPANT_ROLES}")
    part = session.execute(
        select(orm.SourceParticipant)
        .where(orm.SourceParticipant.thread_id == thread_id)
        .where(orm.SourceParticipant.id == participant_id)
        .where(orm.SourceParticipant.workspace_id == scope.workspace_id)
    ).scalar_one_or_none()
    if part is None:
        raise TranscriptError("no such participant")

    person_uuid: uuid.UUID | None = None
    if role == "person":
        if not person_public_id:
            raise TranscriptError("choose a person to map this participant to")
        person = session.execute(
            select(orm.Person)
            .where(orm.Person.workspace_id == scope.workspace_id)
            .where(orm.Person.public_id == person_public_id.strip())
        ).scalar_one_or_none()
        if person is None:
            raise TranscriptError(f"no person {person_public_id!r} in this workspace")
        person_uuid = person.id
    part.role = role
    part.person_id = person_uuid


def apply_participant_mappings(
    session: Session,
    scope: WorkspaceScope,
    thread_id: uuid.UUID,
    mappings: dict[str, tuple[str, str | None]],
) -> list[str]:
    """Save a whole participant->role/person map at once (the transcript
    page has one form, saved when the owner runs extraction). Applies every
    valid entry; returns a list of human-readable problems for the rest."""
    _require_thread(session, scope, thread_id)
    parts = {
        str(p.id): p
        for p in session.execute(
            select(orm.SourceParticipant)
            .where(orm.SourceParticipant.thread_id == thread_id)
            .where(orm.SourceParticipant.workspace_id == scope.workspace_id)
        ).scalars()
    }
    errors: list[str] = []
    for pid, (role, person_public_id) in mappings.items():
        if pid not in parts:
            continue
        try:
            map_participant(
                session,
                scope,
                thread_id,
                uuid.UUID(pid),
                role=role,
                person_public_id=person_public_id,
            )
        except (TranscriptError, ValueError) as exc:
            errors.append(f"{parts[pid].display_name}: {exc}")
    return errors


def delete_thread(session: Session, scope: WorkspaceScope, thread_id: uuid.UUID) -> None:
    thread = _require_thread(session, scope, thread_id)
    session.delete(thread)  # cascades to participants + messages; extracted facts stay
