"""First-run wizard + dashboard auth: single owner, no ownership seizure,
password hashing, sessions, CSRF, throttling (spec 25.1, 27, 32.7)."""

from __future__ import annotations

import datetime as dt

import pytest

from montauk.db.crypto import verify_password
from montauk.services.auth import (
    AccountLocked,
    LoginThrottle,
    authenticate,
    create_session,
    destroy_session,
    resolve_session,
)
from montauk.services.workspace import (
    DeploymentAlreadyInitialized,
    bootstrap_deployment,
    is_initialized,
)


class TestFirstRun:
    def test_bootstrap_creates_owner_workspace_settings_and_sequence(self, db_session):
        assert not is_initialized(db_session)
        user, ws = bootstrap_deployment(
            db_session,
            email="Owner@Example.com",
            password="correct horse battery staple",
            workspace_name="My People",
        )
        db_session.flush()

        assert is_initialized(db_session)
        assert user.email == "owner@example.com"
        assert user.password_hash != "correct horse battery staple"
        assert verify_password(user.password_hash, "correct horse battery staple")
        assert ws.slug == "my-people"
        assert ws.settings.review_threshold == "automatic_all"
        assert ws.settings.allowed_reviewers == "human_only"

    def test_first_run_adopts_a_workspace_left_by_a_pre_setup_migration(self, db_session):
        from montauk.db.repositories import Actor, PeopleRepository, WorkspaceScope
        from montauk.models import Person
        from montauk.services.workspace import get_or_create_workspace

        ws = get_or_create_workspace(db_session, "Imported")
        PeopleRepository(WorkspaceScope(db_session, ws.id, Actor("migration"))).create(
            Person(id="P0001", name="Already Here"), record_revision=False
        )
        db_session.flush()

        user, adopted = bootstrap_deployment(
            db_session, email="o@example.com", password="pw-1234567890", workspace_name="Mine"
        )
        db_session.flush()
        assert adopted.id == ws.id
        assert adopted.name == "Mine"
        assert PeopleRepository(WorkspaceScope(db_session, adopted.id, Actor("owner"))).count() == 1

    def test_second_first_run_cannot_seize_ownership(self, db_session):
        bootstrap_deployment(db_session, email="a@example.com", password="pw-aaaaaaaaaa", workspace_name="WS")
        db_session.flush()
        with pytest.raises(DeploymentAlreadyInitialized):
            bootstrap_deployment(
                db_session, email="b@example.com", password="pw-bbbbbbbbbb", workspace_name="WS2"
            )


class TestLogin:
    @pytest.fixture
    def owner(self, db_session):
        user, ws = bootstrap_deployment(
            db_session, email="owner@example.com", password="s3cret-passphrase", workspace_name="WS"
        )
        db_session.flush()
        return user, ws

    def test_correct_password_authenticates(self, db_session, owner):
        throttle = LoginThrottle()
        user = authenticate(
            db_session, email="owner@example.com", password="s3cret-passphrase", throttle=throttle
        )
        assert user is not None

    def test_wrong_password_fails_and_counts_towards_lockout(self, db_session, owner):
        throttle = LoginThrottle(max_attempts=3)
        for _ in range(3):
            assert (
                authenticate(db_session, email="owner@example.com", password="nope", throttle=throttle)
                is None
            )
        with pytest.raises(AccountLocked):
            authenticate(
                db_session, email="owner@example.com", password="s3cret-passphrase", throttle=throttle
            )

    def test_lockout_expires(self, db_session, owner):
        throttle = LoginThrottle(max_attempts=2, lockout=dt.timedelta(minutes=15))
        t0 = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        for _ in range(2):
            authenticate(db_session, email="owner@example.com", password="x", throttle=throttle, now=t0)
        assert throttle.locked("owner@example.com", now=t0)
        later = t0 + dt.timedelta(minutes=16)
        assert not throttle.locked("owner@example.com", now=later)

    def test_successful_login_resets_the_counter(self, db_session, owner):
        throttle = LoginThrottle(max_attempts=3)
        authenticate(db_session, email="owner@example.com", password="bad", throttle=throttle)
        authenticate(db_session, email="owner@example.com", password="s3cret-passphrase", throttle=throttle)
        assert not throttle.locked("owner@example.com")


class TestSessions:
    @pytest.fixture
    def owner(self, db_session):
        user, ws = bootstrap_deployment(
            db_session, email="owner@example.com", password="s3cret-passphrase", workspace_name="WS"
        )
        db_session.flush()
        return user, ws

    def test_session_round_trip(self, db_session, owner):
        user, ws = owner
        raw = create_session(db_session, user=user, workspace_id=ws.id)
        db_session.flush()

        ctx = resolve_session(db_session, raw)
        assert ctx is not None
        assert ctx.email == "owner@example.com"
        assert ctx.workspace_id == ws.id
        assert ctx.role == "owner"
        assert ctx.csrf_secret

    def test_unknown_token_resolves_to_none(self, db_session, owner):
        assert resolve_session(db_session, "not-a-real-token") is None

    def test_expired_session_is_rejected_and_deleted(self, db_session, owner):
        user, ws = owner
        raw = create_session(
            db_session,
            user=user,
            workspace_id=ws.id,
            now=dt.datetime(2020, 1, 1, tzinfo=dt.UTC),
        )
        db_session.flush()
        assert resolve_session(db_session, raw) is None
        assert resolve_session(db_session, raw) is None

    def test_destroy_session_logs_out(self, db_session, owner):
        user, ws = owner
        raw = create_session(db_session, user=user, workspace_id=ws.id)
        db_session.flush()
        destroy_session(db_session, raw)
        db_session.flush()
        assert resolve_session(db_session, raw) is None

    def test_raw_token_is_not_stored(self, db_session, owner):
        from montauk.db import models as orm

        user, ws = owner
        raw = create_session(db_session, user=user, workspace_id=ws.id)
        db_session.flush()
        stored = db_session.query(orm.Session).all()
        assert all(raw not in s.token_hash for s in stored)
