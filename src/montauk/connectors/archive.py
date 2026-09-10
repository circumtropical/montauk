"""Write connector messages into the shared source archive.

Reuses ``source_threads`` / ``source_participants`` / ``source_messages`` so
the transcript browser, extraction, and dedup all work unchanged. Dedup key
is the provider message id (spec 15.2); the name fingerprint is kept as a
secondary key for consistency with imported threads.

Nothing here confirms an identity mapping: a connector only ever writes
``suggested_person_id`` and leaves ``role='unmapped'`` (spec 14).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import models as orm
from ..db.repositories import PeopleRepository, WorkspaceScope
from ..ids import normalize_alias
from ..services.transcripts import _fingerprint, _name_matches, _thread_key
from .base import IncomingMessage
from .whatsapp import PROVIDER, jid_to_phone

PLATFORM = "whatsapp"


@dataclass
class ArchiveResult:
    source_thread_id: uuid.UUID
    new_messages: int
    duplicate_messages: int
    awaiting_processing: int


def _phone_match(session: Session, workspace_id: uuid.UUID, jid: str) -> uuid.UUID | None:
    phone = jid_to_phone(jid)
    if not phone:
        return None
    candidates = {phone, phone.lstrip("+")}
    rows = session.execute(
        select(orm.PersonContactMethod.person_id, orm.PersonContactMethod.value_normalized)
        .join(orm.Person, orm.Person.id == orm.PersonContactMethod.person_id)
        .where(orm.PersonContactMethod.workspace_id == workspace_id)
        .where(orm.PersonContactMethod.kind == "phone")
        .where(orm.Person.archived_at.is_(None))
    ).all()
    hits = {pid for pid, norm in rows if norm and (norm in candidates or norm.lstrip("+") in candidates)}
    return next(iter(hits)) if len(hits) == 1 else None


def _ensure_source_thread(
    session: Session,
    ctx: orm.ConnectorThread,
    account: orm.ConnectorAccount,
) -> orm.SourceThread:
    if ctx.source_thread_id:
        thread = session.get(orm.SourceThread, ctx.source_thread_id)
        if thread is not None:
            return thread
    key = _thread_key([ctx.provider_thread_id])
    thread = session.execute(
        select(orm.SourceThread)
        .where(orm.SourceThread.workspace_id == ctx.workspace_id)
        .where(orm.SourceThread.platform == PLATFORM)
        .where(orm.SourceThread.thread_key == key)
    ).scalar_one_or_none()
    if thread is None:
        thread = orm.SourceThread(
            workspace_id=ctx.workspace_id,
            platform=PLATFORM,
            thread_key=key,
            title=ctx.title or ctx.provider_thread_id,
            connector_account_id=account.id,
            provider_thread_id=ctx.provider_thread_id,
        )
        session.add(thread)
        session.flush()
    ctx.source_thread_id = thread.id
    return thread


def _suggest_person(
    session: Session, scope: WorkspaceScope, people: list[orm.Person], display_name: str, jid: str
) -> uuid.UUID | None:
    by_phone = _phone_match(session, scope.workspace_id, jid)
    if by_phone is not None:
        return by_phone
    if display_name:
        hits = _name_matches(people, display_name)
        if len(hits) == 1:
            return hits[0].id
    return None


def _ensure_participant(
    session: Session,
    scope: WorkspaceScope,
    thread: orm.SourceThread,
    account: orm.ConnectorAccount,
    people: list[orm.Person],
    *,
    display_name: str,
    jid: str,
    is_self: bool,
    cache: dict[str, orm.SourceParticipant],
) -> orm.SourceParticipant:
    name = display_name or (jid_to_phone(jid) or jid)
    norm = normalize_alias(name)
    part = cache.get(norm)
    if part is None:
        part = session.execute(
            select(orm.SourceParticipant)
            .where(orm.SourceParticipant.thread_id == thread.id)
            .where(orm.SourceParticipant.display_name_normalized == norm)
        ).scalar_one_or_none()
    if part is None:
        part = orm.SourceParticipant(
            workspace_id=scope.workspace_id,
            thread_id=thread.id,
            display_name=name,
            display_name_normalized=norm,
            role="owner" if is_self else "unmapped",
            source_identity=jid,
        )
        if not is_self:
            part.suggested_person_id = _suggest_person(session, scope, people, name, jid)
        session.add(part)
        session.flush()
    else:
        # Backfill identity / suggestion on a participant first seen via import.
        if not part.source_identity:
            part.source_identity = jid
        if is_self and part.role == "unmapped":
            part.role = "owner"
        if not is_self and part.role == "unmapped" and part.suggested_person_id is None:
            part.suggested_person_id = _suggest_person(session, scope, people, name, jid)
    cache[norm] = part
    return part


def archive_messages(
    session: Session,
    scope: WorkspaceScope,
    account: orm.ConnectorAccount,
    ctx: orm.ConnectorThread,
    messages: list[IncomingMessage],
) -> ArchiveResult:
    """Idempotently write ``messages`` for one enabled connector thread."""
    thread = _ensure_source_thread(session, ctx, account)
    people = list(PeopleRepository(scope).list_people(archived=False))
    key = thread.thread_key

    seen_ids = {
        r
        for r in session.execute(
            select(orm.SourceMessage.provider_message_id)
            .where(orm.SourceMessage.thread_id == thread.id)
            .where(orm.SourceMessage.provider_message_id.is_not(None))
        ).scalars()
    }
    part_cache: dict[str, orm.SourceParticipant] = {}
    new = dup = 0
    for m in messages:
        if m.provider_message_id in seen_ids:
            dup += 1
            continue
        seen_ids.add(m.provider_message_id)
        is_self = m.from_me
        sender_label = (
            account.self_display_name or "You"
            if is_self
            else (m.sender_display_name or jid_to_phone(m.sender_identity) or m.sender_identity)
        )
        _ensure_participant(
            session,
            scope,
            thread,
            account,
            people,
            display_name="" if is_self else sender_label,
            jid=m.sender_identity if not is_self else (account.self_identity or m.sender_identity),
            is_self=is_self,
            cache=part_cache,
        )
        text = m.text or ("[media omitted]" if m.kind == "media" else "")
        skip = m.kind in ("system", "revoked") or (m.kind == "media" and not m.text)
        fp = _fingerprint(key, normalize_alias(sender_label), m.sent_at, text or m.provider_message_id)
        session.add(
            orm.SourceMessage(
                workspace_id=scope.workspace_id,
                thread_id=thread.id,
                connector_account_id=account.id,
                fingerprint=fp,
                provider_message_id=m.provider_message_id,
                direction=m.direction,
                sender_name=sender_label,
                sender_normalized=normalize_alias(sender_label),
                sender_identity=m.sender_identity,
                sent_at=m.sent_at,
                text=text,
                is_system=m.is_system,
                media_omitted=m.kind == "media",
                content_omitted=m.content_omitted,
                processing_status="skipped" if skip else "awaiting_processing",
            )
        )
        new += 1

    thread.updated_at = dt.datetime.now(dt.UTC)
    if messages:
        latest = max(m.sent_at for m in messages)
        if ctx.last_message_at is None or latest > ctx.last_message_at:
            ctx.last_message_at = latest
    session.flush()

    awaiting = int(
        session.execute(
            select(func.count(orm.SourceMessage.id))
            .where(orm.SourceMessage.thread_id == thread.id)
            .where(orm.SourceMessage.processing_status == "awaiting_processing")
        ).scalar_one()
    )
    return ArchiveResult(
        source_thread_id=thread.id,
        new_messages=new,
        duplicate_messages=dup,
        awaiting_processing=awaiting,
    )


__all__ = ["ArchiveResult", "archive_messages", "PLATFORM", "PROVIDER"]
