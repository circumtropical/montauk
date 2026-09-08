"""Migration gate: head applies from empty, and upgrade/downgrade/upgrade
round-trips cleanly (spec 33 gate 6)."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import Engine

from montauk.db.models import Base
from montauk.db.schema_ops import (
    current_revision,
    downgrade_to,
    head_revision,
    is_up_to_date,
    upgrade_to_head,
)


def test_head_matches_orm_metadata(engine: Engine) -> None:
    # Every ORM table exists after upgrade to head.
    inspector = sa.inspect(engine)
    actual = set(inspector.get_table_names())
    expected = set(Base.metadata.tables) | {"alembic_version"}
    assert expected <= actual


def test_up_to_date(database_url: str, engine: Engine) -> None:
    assert is_up_to_date(database_url)
    assert current_revision(database_url) == head_revision(database_url)


def test_downgrade_then_upgrade_roundtrips(database_url: str, engine: Engine) -> None:
    downgrade_to("base", database_url)
    inspector = sa.inspect(engine)
    assert "people" not in inspector.get_table_names()

    upgrade_to_head(database_url)
    inspector = sa.inspect(engine)
    assert "people" in inspector.get_table_names()
    assert is_up_to_date(database_url)
