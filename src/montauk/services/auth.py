"""Dashboard authentication: password login with failed-attempt throttling,
opaque server-side sessions, and CSRF secrets (spec 25.1, 27).

Two-factor auth is deferred (spec 25.1). Sessions are bounded; only a
SHA-256 hash of the session token is stored.
"""

from __future__ import annotations

import datetime as dt
import secrets
import uuid
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..db import models as orm
from ..db.crypto import hash_password as _hash_password
from ..db.crypto import hash_token, needs_rehash, verify_password

SESSION_TTL = dt.timedelta(hours=12)
SESSION_COOKIE = "montauk_session"


@dataclass(frozen=True)
class AuthContext:
    user_id: uuid.UUID
    email: str
    workspace_id: uuid.UUID
    workspace_slug: str
    role: str
    csrf_secret: str
    session_token_hash: str


class LoginThrottle:
    """Per-(key) sliding failed-attempt counter with temporary lockout.

    In-process state -- correct for the single-process self-hosted
    deployment. A later multi-process topology moves this to PostgreSQL.
    """

    def __init__(self, *, max_attempts: int = 5, lockout: dt.timedelta = dt.timedelta(minutes=15)):
        self.max_attempts = max_attempts
        self.lockout = lockout
        self._fails: dict[str, list[dt.datetime]] = defaultdict(list)

    def _prune(self, key: str, now: dt.datetime) -> None:
        cutoff = now - self.lockout
        self._fails[key] = [t for t in self._fails[key] if t > cutoff]

    def locked(self, key: str, *, now: dt.datetime | None = None) -> bool:
        now = now or dt.datetime.now(dt.UTC)
        self._prune(key, now)
        return len(self._fails[key]) >= self.max_attempts

    def record_failure(self, key: str, *, now: dt.datetime | None = None) -> None:
        now = now or dt.datetime.now(dt.UTC)
        self._fails[key].append(now)
        self._prune(key, now)

    def reset(self, key: str) -> None:
        self._fails.pop(key, None)


class AccountLocked(RuntimeError):
    pass


def authenticate(
    session: Session,
    *,
    email: str,
    password: str,
    throttle: LoginThrottle,
    throttle_key: str | None = None,
    now: dt.datetime | None = None,
) -> orm.User | None:
    now = now or dt.datetime.now(dt.UTC)
    key = throttle_key or email.strip().lower()
    if throttle.locked(key, now=now):
        raise AccountLocked("too many failed attempts; try again later")

    user = session.execute(
        select(orm.User).where(orm.User.email == email.strip().lower())
    ).scalar_one_or_none()
    if user is None or not verify_password(user.password_hash, password):
        throttle.record_failure(key, now=now)
        return None

    throttle.reset(key)
    if needs_rehash(user.password_hash):
        user.password_hash = _hash_password(password)
    user.last_login_at = now
    return user


MIN_PASSWORD_LEN = 10


class PasswordChangeError(ValueError):
    pass


def change_password(
    session: Session,
    *,
    user_id: uuid.UUID,
    current_password: str,
    new_password: str,
    new_password_confirm: str,
    keep_session_token_hash: str | None = None,
) -> None:
    """Verify the current password, set a new one, and log out every other
    session for this user (spec 27). The caller's own session is kept."""
    user = session.get(orm.User, user_id)
    if user is None:
        raise PasswordChangeError("user not found")
    if not verify_password(user.password_hash, current_password):
        raise PasswordChangeError("current password is incorrect")
    if len(new_password) < MIN_PASSWORD_LEN:
        raise PasswordChangeError(f"new password must be at least {MIN_PASSWORD_LEN} characters")
    if new_password != new_password_confirm:
        raise PasswordChangeError("new passwords do not match")
    if verify_password(user.password_hash, new_password):
        raise PasswordChangeError("new password must differ from the current one")
    user.password_hash = _hash_password(new_password)
    stmt = delete(orm.Session).where(orm.Session.user_id == user_id)
    if keep_session_token_hash:
        stmt = stmt.where(orm.Session.token_hash != keep_session_token_hash)
    session.execute(stmt)


def create_session(
    session: Session, *, user: orm.User, workspace_id: uuid.UUID, now: dt.datetime | None = None
) -> str:
    now = now or dt.datetime.now(dt.UTC)
    raw = secrets.token_urlsafe(32)
    session.add(
        orm.Session(
            token_hash=hash_token(raw),
            user_id=user.id,
            workspace_id=workspace_id,
            csrf_secret=secrets.token_urlsafe(16),
            created_at=now,
            expires_at=now + SESSION_TTL,
            last_seen_at=now,
        )
    )
    return raw


def resolve_session(
    session: Session, raw_token: str | None, *, now: dt.datetime | None = None
) -> AuthContext | None:
    if not raw_token:
        return None
    now = now or dt.datetime.now(dt.UTC)
    row = session.get(orm.Session, hash_token(raw_token))
    if row is None:
        return None
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=dt.UTC)
    if expires_at <= now:
        session.delete(row)
        return None

    membership = session.execute(
        select(orm.WorkspaceMembership)
        .where(orm.WorkspaceMembership.workspace_id == row.workspace_id)
        .where(orm.WorkspaceMembership.user_id == row.user_id)
    ).scalar_one_or_none()
    if membership is None:
        return None
    workspace = session.get(orm.Workspace, row.workspace_id)
    user = session.get(orm.User, row.user_id)
    if workspace is None or user is None:
        return None

    row.last_seen_at = now
    return AuthContext(
        user_id=user.id,
        email=user.email,
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        role=membership.role,
        csrf_secret=row.csrf_secret,
        session_token_hash=row.token_hash,
    )


def destroy_session(session: Session, raw_token: str | None) -> None:
    if not raw_token:
        return
    session.execute(delete(orm.Session).where(orm.Session.token_hash == hash_token(raw_token)))


def purge_expired_sessions(session: Session, *, now: dt.datetime | None = None) -> int:
    now = now or dt.datetime.now(dt.UTC)
    result = session.execute(delete(orm.Session).where(orm.Session.expires_at <= now))
    return int(getattr(result, "rowcount", 0) or 0)
