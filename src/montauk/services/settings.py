"""Workspace + review-policy settings updates (spec 25.6, 19, Appendix B)."""

from __future__ import annotations

import re
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from ..db import models as orm

REVIEW_THRESHOLDS = ("automatic_all", "review_uncertain", "review_all")
ALLOWED_REVIEWERS = ("human_only", "human_or_authorized_agent")
DEPLOYMENT_PROFILES = ("private", "public")
HISTORICAL_INGESTION = ("future_only", "since_date", "all_history")

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class SettingsError(ValueError):
    pass


def _require(value: str, allowed: tuple[str, ...], label: str) -> str:
    if value not in allowed:
        raise SettingsError(f"{label} must be one of: {', '.join(allowed)}")
    return value


def update_workspace(
    session: Session,
    workspace_id: uuid.UUID,
    *,
    name: str,
    deployment_profile: str,
    public_url: str,
) -> None:
    ws = session.get(orm.Workspace, workspace_id)
    if ws is None:
        raise SettingsError("workspace not found")
    name = name.strip()
    if not name:
        raise SettingsError("workspace name is required")
    _require(deployment_profile, DEPLOYMENT_PROFILES, "deployment profile")
    public_url = public_url.strip()
    if public_url and not re.match(r"^https?://[^\s]+$", public_url):
        raise SettingsError("public URL must be an http(s) URL, or blank")
    if deployment_profile == "public" and not public_url.startswith("https://"):
        raise SettingsError("a public deployment needs an https:// public URL")
    ws.name = name
    ws.deployment_profile = deployment_profile
    ws.public_url = public_url or None


def update_review_policy(
    session: Session,
    workspace_id: uuid.UUID,
    *,
    review_threshold: str,
    allowed_reviewers: str,
    timezone: str,
    daily_extraction_time: str,
    historical_ingestion_default: str,
    agent_transcript_access: bool,
) -> None:
    s = session.get(orm.WorkspaceSettings, workspace_id)
    if s is None:
        s = orm.WorkspaceSettings(workspace_id=workspace_id)
        session.add(s)
    s.review_threshold = _require(review_threshold, REVIEW_THRESHOLDS, "review threshold")
    s.allowed_reviewers = _require(allowed_reviewers, ALLOWED_REVIEWERS, "allowed reviewers")
    s.historical_ingestion_default = _require(
        historical_ingestion_default, HISTORICAL_INGESTION, "historical ingestion default"
    )
    timezone = timezone.strip() or "UTC"
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise SettingsError(
            f"unknown timezone {timezone!r} (use an IANA name like 'America/New_York')"
        ) from exc
    s.timezone = timezone
    if not _TIME_RE.match(daily_extraction_time.strip()):
        raise SettingsError("daily extraction time must be HH:MM (24-hour)")
    s.daily_extraction_time = daily_extraction_time.strip()
    s.agent_transcript_access = bool(agent_transcript_access)
