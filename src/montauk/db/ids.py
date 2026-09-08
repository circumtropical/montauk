"""Per-workspace permanent public person-id allocation (spec 8.5).

``P[0-9]{4,}`` ids, unique within a workspace, allocated transactionally
from a monotonic high-water mark that is never lowered and never reused.
"""

from __future__ import annotations

import uuid

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..ids import format_person_id
from .models import PersonIdSequence


def _ensure_row(session: Session, workspace_id: uuid.UUID) -> None:
    session.execute(
        pg_insert(PersonIdSequence)
        .values(workspace_id=workspace_id, last_allocated=0)
        .on_conflict_do_nothing(index_elements=["workspace_id"])
    )


def allocate_public_id(session: Session, workspace_id: uuid.UUID) -> str:
    """Advance the workspace's high-water mark by one and return the new
    canonical id. Serialized by the row lock taken by the UPDATE; safe to
    call concurrently from separate transactions."""
    _ensure_row(session, workspace_id)
    row = session.get(PersonIdSequence, workspace_id, with_for_update=True)
    assert row is not None
    row.last_allocated += 1
    session.flush()
    return format_person_id(row.last_allocated)


def high_water(session: Session, workspace_id: uuid.UUID) -> int:
    row = session.get(PersonIdSequence, workspace_id)
    return row.last_allocated if row is not None else 0


def ensure_high_water_at_least(session: Session, workspace_id: uuid.UUID, value: int) -> bool:
    """Raise the mark to ``value`` if currently lower (migration: advance past
    every imported id, spec 30.2). Never lowers it. Returns True if advanced."""
    _ensure_row(session, workspace_id)
    row = session.get(PersonIdSequence, workspace_id, with_for_update=True)
    assert row is not None
    if value > row.last_allocated:
        row.last_allocated = value
        session.flush()
        return True
    return False


__all__ = ["allocate_public_id", "ensure_high_water_at_least", "high_water"]
