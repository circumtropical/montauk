"""Agent authentication and role authorization (spec section 30).

Agent credentials are NOT derived/disposable data -- unlike
relationships.sqlite and the vector index, they cannot be reconstructed
from Markdown, so they live in their own store (data/auth/
credentials.sqlite by convention) that rebuild-index/rebuild-vectors
must never touch.

Identity resolution is transport-specific: on the HTTP transport, each
call carries its own `Authorization: Bearer <token>` header, read via
the SDK's `Context.headers` (populated per-request); on stdio there is
exactly one long-lived client, so identity is resolved once at server
startup from an environment variable instead (`Context.headers` is
documented to be `None` there).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .errors import PermissionDeniedError
from .ids import slugify

Role = Literal["read_only", "read_write"]

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agents (
  agent_id     TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  role         TEXT NOT NULL CHECK (role IN ('read_only', 'read_write')),
  token_hash   TEXT NOT NULL UNIQUE,
  token_prefix TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  revoked_at   TEXT
);
"""

TOKEN_PREFIX_LEN = 12
ENV_VAR_AGENT_TOKEN = "MONTAUK_AGENT_TOKEN"
AUTHORIZATION_HEADER = "authorization"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _next_agent_id(name: str, existing_ids: set[str]) -> str:
    base = slugify(name)
    if base not in existing_ids:
        return base
    suffix = 2
    while f"{base}-{suffix}" in existing_ids:
        suffix += 1
    return f"{base}-{suffix}"


@dataclass(frozen=True, slots=True)
class AgentIdentity:
    agent_id: str
    name: str
    role: Role


@dataclass(frozen=True, slots=True)
class AgentRecord:
    agent_id: str
    name: str
    role: Role
    token_prefix: str
    created_at: str
    revoked_at: str | None

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None


def _record_from_row(row: sqlite3.Row) -> AgentRecord:
    return AgentRecord(
        agent_id=row["agent_id"],
        name=row["name"],
        role=row["role"],
        token_prefix=row["token_prefix"],
        created_at=row["created_at"],
        revoked_at=row["revoked_at"],
    )


class CredentialStore:
    """Owns data/auth/credentials.sqlite exclusively. Never opened or
    written by sqlite_index.py or semantic_index.py."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> CredentialStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def create_agent(self, name: str, role: Role) -> tuple[AgentRecord, str]:
        """Generate and persist a new credential. Returns the record and
        the raw bearer token -- the only moment the raw token is ever
        available; only its hash is stored."""
        existing_ids = {row["agent_id"] for row in self._conn.execute("SELECT agent_id FROM agents")}
        agent_id = _next_agent_id(name, existing_ids)
        token = f"mtk_{secrets.token_urlsafe(32)}"
        created_at = dt.datetime.now(dt.UTC).isoformat()
        self._conn.execute(
            "INSERT INTO agents (agent_id, name, role, token_hash, token_prefix, created_at, revoked_at) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (agent_id, name, role, _hash_token(token), token[:TOKEN_PREFIX_LEN], created_at),
        )
        self._conn.commit()
        return (
            AgentRecord(
                agent_id=agent_id,
                name=name,
                role=role,
                token_prefix=token[:TOKEN_PREFIX_LEN],
                created_at=created_at,
                revoked_at=None,
            ),
            token,
        )

    def revoke_agent(self, agent_id: str) -> bool:
        cursor = self._conn.execute(
            "UPDATE agents SET revoked_at = ? WHERE agent_id = ? AND revoked_at IS NULL",
            (dt.datetime.now(dt.UTC).isoformat(), agent_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def list_agents(self) -> list[AgentRecord]:
        rows = self._conn.execute("SELECT * FROM agents ORDER BY created_at").fetchall()
        return [_record_from_row(r) for r in rows]

    def verify_token(self, token: str) -> AgentIdentity | None:
        """Return the agent's identity for a valid, non-revoked token, or
        None for an unknown/revoked/malformed token. Never raises on bad
        input -- callers treat None as "unauthenticated"."""
        if not token:
            return None
        row = self._conn.execute(
            "SELECT agent_id, name, role, revoked_at FROM agents WHERE token_hash = ?",
            (_hash_token(token),),
        ).fetchone()
        if row is None or row["revoked_at"] is not None:
            return None
        return AgentIdentity(agent_id=row["agent_id"], name=row["name"], role=row["role"])


def extract_bearer_token(headers: dict[str, str] | None) -> str | None:
    if not headers:
        return None
    value = None
    for key, val in headers.items():
        if key.lower() == AUTHORIZATION_HEADER:
            value = val
            break
    if not value or not value.lower().startswith("bearer "):
        return None
    return value[len("bearer ") :].strip() or None


def resolve_http_identity(credential_store: CredentialStore, headers: dict[str, str] | None) -> AgentIdentity | None:
    token = extract_bearer_token(headers)
    if token is None:
        return None
    return credential_store.verify_token(token)


def resolve_stdio_identity(credential_store: CredentialStore, token: str | None) -> AgentIdentity | None:
    if not token:
        return None
    return credential_store.verify_token(token)


def require_write(identity: AgentIdentity | None) -> None:
    """Raise PermissionDeniedError unless `identity` is an authenticated
    read_write agent. `identity=None` means "auth enforcement is active
    but no valid credential was resolved for this call" -- callers that
    haven't wired auth at all simply never call this."""
    if identity is None:
        raise PermissionDeniedError("no valid agent credential presented for this request")
    if identity.role != "read_write":
        raise PermissionDeniedError(
            f"agent {identity.agent_id!r} has role {identity.role!r}; read_write is required for this operation"
        )
