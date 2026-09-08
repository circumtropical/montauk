"""Manual agent token management (spec 22.2).

The raw token is returned once at creation and never stored -- only its
SHA-256 hash. OAuth-style dynamic client registration is a later increment.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import models as orm
from ..db.base import utcnow
from ..db.crypto import hash_token, new_token, token_prefix

# Named capabilities (spec 21). transcript_read and protected_override are
# disabled by default and must be chosen explicitly.
CAPABILITIES = (
    "memory_read",
    "memory_write",
    "review_proposals",
    "process_pending",
    "transcript_read",
    "protected_override",
    "single_person_delete",
)
DEFAULT_CAPABILITIES = ("memory_read", "memory_write")


class AgentCredentialError(ValueError):
    pass


def list_credentials(session: Session, workspace_id: uuid.UUID) -> list[orm.AgentCredential]:
    return list(
        session.execute(
            select(orm.AgentCredential)
            .where(orm.AgentCredential.workspace_id == workspace_id)
            .order_by(orm.AgentCredential.created_at.desc())
        ).scalars()
    )


def create_credential(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    name: str,
    capabilities: list[str],
    created_by: uuid.UUID | None = None,
) -> tuple[orm.AgentCredential, str]:
    name = name.strip()
    if not name:
        raise AgentCredentialError("name is required")
    unknown = sorted(set(capabilities) - set(CAPABILITIES))
    if unknown:
        raise AgentCredentialError(f"unknown capabilities: {', '.join(unknown)}")
    if not capabilities:
        raise AgentCredentialError("select at least one capability")
    exists = session.execute(
        select(orm.AgentCredential.id)
        .where(orm.AgentCredential.workspace_id == workspace_id)
        .where(orm.AgentCredential.name == name)
    ).first()
    if exists:
        raise AgentCredentialError(f"an agent credential named {name!r} already exists")

    raw = new_token("mtk_")
    cred = orm.AgentCredential(
        workspace_id=workspace_id,
        name=name,
        token_hash=hash_token(raw),
        token_prefix=token_prefix(raw),
        capabilities=sorted(set(capabilities)),
        created_by=created_by,
    )
    session.add(cred)
    session.flush()
    return cred, raw


def revoke_credential(session: Session, *, workspace_id: uuid.UUID, credential_id: uuid.UUID) -> bool:
    cred = session.get(orm.AgentCredential, credential_id)
    if cred is None or cred.workspace_id != workspace_id or cred.revoked_at is not None:
        return False
    cred.revoked_at = utcnow()
    return True


def rotate_credential(session: Session, *, workspace_id: uuid.UUID, credential_id: uuid.UUID) -> str | None:
    cred = session.get(orm.AgentCredential, credential_id)
    if cred is None or cred.workspace_id != workspace_id or cred.revoked_at is not None:
        return None
    raw = new_token("mtk_")
    cred.token_hash = hash_token(raw)
    cred.token_prefix = token_prefix(raw)
    cred.updated_at = dt.datetime.now(dt.UTC)
    return raw
