"""Programmatic Alembic operations: run migrations from the CLI, the
first-run wizard, and tests without shelling out to the ``alembic`` script.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine

from .engine import resolve_url

_ALEMBIC_INI = Path(__file__).resolve().parents[3] / "alembic.ini"


def _config(url: str | None) -> Config:
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(Path(__file__).parent / "alembic"))
    resolved = resolve_url(url)
    cfg.set_main_option("sqlalchemy.url", resolved)
    cfg.attributes["configure_logger"] = False
    return cfg


def upgrade_to_head(url: str | None = None) -> None:
    command.upgrade(_config(url), "head")


def downgrade_to(revision: str, url: str | None = None) -> None:
    command.downgrade(_config(url), revision)


def current_revision(url: str | None = None) -> str | None:
    engine = create_engine(resolve_url(url))
    try:
        with engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()


def head_revision(url: str | None = None) -> str:
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_config(url))
    head = script.get_current_head()
    assert head is not None
    return head


def is_up_to_date(url: str | None = None) -> bool:
    return current_revision(url) == head_revision(url)
