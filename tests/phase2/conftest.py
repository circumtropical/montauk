"""PostgreSQL-backed test fixtures for the Phase 2 canonical store.

A database is obtained in one of two ways:

* ``MONTAUK_TEST_DATABASE_URL`` (or ``MONTAUK_DATABASE_URL``) in the
  environment -- used as-is (CI provides a ``postgres`` service this way);
* otherwise a disposable ``postgres:17-alpine`` container via
  testcontainers (local dev; requires Docker).

Alembic migrations are run once against ``head``. Each test starts from a
clean set of tables (every table truncated after the test).
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from montauk.db.engine import create_db_engine, normalize_url
from montauk.db.schema_ops import upgrade_to_head

_TABLES_IN_FK_ORDER: list[str] = []  # filled after reflect


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    env = os.environ.get("MONTAUK_TEST_DATABASE_URL") or os.environ.get("MONTAUK_DATABASE_URL")
    if env:
        yield normalize_url(env)
        return
    try:
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:  # pragma: no cover
        pytest.skip("no MONTAUK_TEST_DATABASE_URL and testcontainers not installed")
    container = PostgresContainer("postgres:17-alpine", driver="psycopg")
    container.start()
    try:
        yield normalize_url(container.get_connection_url())
    finally:
        container.stop()


@pytest.fixture(scope="session")
def engine(database_url: str) -> Iterator[Engine]:
    eng = create_db_engine(database_url)
    upgrade_to_head(database_url)
    with eng.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "AND tablename <> 'alembic_version'"
            )
        )
        _TABLES_IN_FK_ORDER[:] = [r[0] for r in rows]
    yield eng
    eng.dispose()


@pytest.fixture
def _truncate(engine: Engine) -> Iterator[None]:
    yield
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "TRUNCATE TABLE "
                + ", ".join(f'"{t}"' for t in _TABLES_IN_FK_ORDER)
                + " RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture
def session_maker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@pytest.fixture
def db_session(session_maker: sessionmaker[Session], _truncate: None) -> Iterator[Session]:
    session = session_maker()
    try:
        yield session
        try:
            session.commit()
        except Exception:
            session.rollback()
    finally:
        session.close()


@pytest.fixture
def make_workspace(db_session: Session):
    from montauk.db import models as orm

    def _make(name: str = "Test Workspace") -> orm.Workspace:
        from montauk.services.workspace import get_or_create_workspace

        ws = get_or_create_workspace(db_session, name)
        db_session.flush()
        return ws

    return _make


@pytest.fixture
def workspace(make_workspace):
    return make_workspace("Alpha")


@pytest.fixture
def other_workspace(make_workspace):
    return make_workspace("Beta")


@pytest.fixture
def scope(db_session: Session, workspace):
    from montauk.db.repositories import Actor, WorkspaceScope

    return WorkspaceScope(db_session, workspace.id, Actor("owner", "owner@example.com"))


@pytest.fixture
def other_scope(db_session: Session, other_workspace):
    from montauk.db.repositories import Actor, WorkspaceScope

    return WorkspaceScope(db_session, other_workspace.id, Actor("owner", "owner@example.com"))


@pytest.fixture
def secret_box():
    import secrets as _secrets

    from montauk.db.crypto import SecretBox

    return SecretBox(key=_secrets.token_bytes(32))


@pytest.fixture
def web_app(session_maker, engine, _truncate):
    from montauk.services.auth import LoginThrottle
    from montauk.web.app import create_app

    return create_app(session_maker, throttle=LoginThrottle(max_attempts=5), secure_cookies=False)


@pytest.fixture
def client(web_app):
    from starlette.testclient import TestClient

    with TestClient(web_app) as c:
        yield c
