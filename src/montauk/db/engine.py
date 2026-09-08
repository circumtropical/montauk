"""Engine / session-factory construction and DATABASE_URL resolution.

The canonical PostgreSQL DSN comes from configuration or the environment
(``MONTAUK_DATABASE_URL`` / ``DATABASE_URL``), never from a checked-in
default (spec 26.2). The driver is normalized to ``psycopg`` (psycopg 3).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

ENV_VARS = ("MONTAUK_DATABASE_URL", "DATABASE_URL")


class DatabaseNotConfigured(RuntimeError):
    pass


def normalize_url(url: str) -> str:
    """Force the psycopg (v3) driver so a bare ``postgresql://`` or a
    ``postgresql+psycopg2://`` DSN still works."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    if url.startswith("postgresql+psycopg2://"):
        url = "postgresql+psycopg://" + url[len("postgresql+psycopg2://") :]
    return url


def resolve_url(explicit: str | None = None) -> str:
    if explicit:
        return normalize_url(explicit)
    for var in ENV_VARS:
        if os.environ.get(var):
            return normalize_url(os.environ[var])
    raise DatabaseNotConfigured(
        "no PostgreSQL DSN configured; set MONTAUK_DATABASE_URL "
        "(e.g. postgresql://user:pass@host:5432/montauk)"
    )


def create_db_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    return create_engine(
        resolve_url(url),
        echo=echo,
        pool_pre_ping=True,
        future=True,
    )


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Transaction boundary: commit on success, roll back on any exception."""
    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
