"""Workspace + owner lifecycle: the first-run wizard and workspace lookup
(spec 7, 25.1)."""

from __future__ import annotations

import re
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import models as orm
from ..db.crypto import hash_password

_SLUG_RE = re.compile(r"[^a-z0-9]+")


class DeploymentAlreadyInitialized(RuntimeError):
    """A second first-run attempt must not be able to seize ownership
    (spec 32.7)."""


def slugify_workspace(name: str) -> str:
    slug = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return slug or "workspace"


def is_initialized(session: Session) -> bool:
    return session.execute(select(func.count(orm.User.id))).scalar_one() > 0


def _unique_slug(session: Session, base: str) -> str:
    existing = set(session.execute(select(orm.Workspace.slug)).scalars())
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def bootstrap_deployment(
    session: Session,
    *,
    email: str,
    password: str,
    workspace_name: str,
    public_url: str | None = None,
    deployment_profile: str | None = None,
) -> tuple[orm.User, orm.Workspace]:
    """Create the single owner user, the initial workspace, its settings,
    and the person-id sequence, in one transaction. Idempotency is the
    caller's job via :func:`is_initialized`; this raises if a user exists."""
    if is_initialized(session):
        raise DeploymentAlreadyInitialized("this deployment already has an owner")

    user = orm.User(email=email.strip().lower(), password_hash=hash_password(password))
    session.add(user)
    session.flush()

    # If a migration ran before first-run, exactly one member-less workspace
    # already exists -- adopt it (and its imported people) rather than
    # stranding the data in an unreachable workspace.
    orphans = list(session.execute(select(orm.Workspace)).scalars())
    adopt = orphans[0] if len(orphans) == 1 else None

    if adopt is not None:
        workspace = adopt
        workspace.name = workspace_name.strip()
        if public_url:
            workspace.public_url = public_url
        if deployment_profile:
            workspace.deployment_profile = deployment_profile
    else:
        workspace = orm.Workspace(
            slug=_unique_slug(session, slugify_workspace(workspace_name)),
            name=workspace_name.strip(),
            public_url=public_url,
            deployment_profile=deployment_profile,
        )
        session.add(workspace)
        session.flush()

    session.add(orm.WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="owner"))
    if session.get(orm.WorkspaceSettings, workspace.id) is None:
        session.add(orm.WorkspaceSettings(workspace_id=workspace.id))
    if session.get(orm.PersonIdSequence, workspace.id) is None:
        session.add(orm.PersonIdSequence(workspace_id=workspace.id, last_allocated=0))
    session.flush()
    return user, workspace


def get_workspace_by_slug(session: Session, slug: str) -> orm.Workspace | None:
    return session.execute(select(orm.Workspace).where(orm.Workspace.slug == slug)).scalar_one_or_none()


def get_or_create_workspace(session: Session, name: str, *, public_url: str | None = None) -> orm.Workspace:
    """Used by the migrator's ``--workspace NAME``. Matches an existing
    workspace by slug, otherwise creates it with settings + id sequence."""
    slug = slugify_workspace(name)
    existing = get_workspace_by_slug(session, slug)
    if existing is not None:
        return existing
    workspace = orm.Workspace(slug=_unique_slug(session, slug), name=name.strip(), public_url=public_url)
    session.add(workspace)
    session.flush()
    session.add(orm.WorkspaceSettings(workspace_id=workspace.id))
    session.add(orm.PersonIdSequence(workspace_id=workspace.id, last_allocated=0))
    session.flush()
    return workspace


def owner_membership(session: Session, workspace_id: uuid.UUID) -> orm.WorkspaceMembership | None:
    return session.execute(
        select(orm.WorkspaceMembership)
        .where(orm.WorkspaceMembership.workspace_id == workspace_id)
        .where(orm.WorkspaceMembership.role == "owner")
    ).scalar_one_or_none()
