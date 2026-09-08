"""Workspace / review-policy settings validation + updates (spec 25.6)."""

from __future__ import annotations

import pytest

from montauk.db import models as orm
from montauk.services.settings import SettingsError, update_review_policy, update_workspace
from montauk.services.workspace import bootstrap_deployment


@pytest.fixture
def ws(db_session):
    _user, workspace = bootstrap_deployment(
        db_session, email="o@example.com", password="pw-1234567890", workspace_name="WS"
    )
    db_session.flush()
    return workspace


class TestWorkspaceUpdate:
    def test_updates_name_profile_and_url(self, db_session, ws):
        update_workspace(
            db_session,
            ws.id,
            name="  Renamed  ",
            deployment_profile="public",
            public_url="https://m.example.com",
        )
        db_session.flush()
        row = db_session.get(orm.Workspace, ws.id)
        assert row.name == "Renamed"
        assert row.deployment_profile == "public"
        assert row.public_url == "https://m.example.com"

    def test_blank_name_rejected(self, db_session, ws):
        with pytest.raises(SettingsError):
            update_workspace(db_session, ws.id, name="  ", deployment_profile="private", public_url="")

    def test_public_profile_requires_https_url(self, db_session, ws):
        with pytest.raises(SettingsError):
            update_workspace(
                db_session, ws.id, name="X", deployment_profile="public", public_url="http://m.example.com"
            )

    def test_bad_profile_rejected(self, db_session, ws):
        with pytest.raises(SettingsError):
            update_workspace(db_session, ws.id, name="X", deployment_profile="hybrid", public_url="")

    def test_blank_url_is_allowed_and_stored_as_null(self, db_session, ws):
        update_workspace(db_session, ws.id, name="X", deployment_profile="private", public_url="   ")
        db_session.flush()
        assert db_session.get(orm.Workspace, ws.id).public_url is None


class TestReviewPolicyUpdate:
    def _apply(self, session, ws_id, **over):
        kw = dict(
            review_threshold="review_uncertain",
            allowed_reviewers="human_or_authorized_agent",
            timezone="America/New_York",
            daily_extraction_time="07:30",
            historical_ingestion_default="future_only",
            agent_transcript_access=True,
        )
        kw.update(over)
        update_review_policy(session, ws_id, **kw)

    def test_valid_update(self, db_session, ws):
        self._apply(db_session, ws.id)
        db_session.flush()
        s = db_session.get(orm.WorkspaceSettings, ws.id)
        assert s.review_threshold == "review_uncertain"
        assert s.allowed_reviewers == "human_or_authorized_agent"
        assert s.timezone == "America/New_York"
        assert s.daily_extraction_time == "07:30"
        assert s.agent_transcript_access is True

    @pytest.mark.parametrize(
        "field,bad",
        [
            ("review_threshold", "sometimes"),
            ("allowed_reviewers", "anyone"),
            ("timezone", "Mars/Olympus"),
            ("daily_extraction_time", "7:30"),
            ("daily_extraction_time", "25:00"),
            ("historical_ingestion_default", "maybe"),
        ],
    )
    def test_invalid_values_rejected(self, db_session, ws, field, bad):
        with pytest.raises(SettingsError):
            self._apply(db_session, ws.id, **{field: bad})

    def test_blank_timezone_defaults_to_utc(self, db_session, ws):
        self._apply(db_session, ws.id, timezone="")
        db_session.flush()
        assert db_session.get(orm.WorkspaceSettings, ws.id).timezone == "UTC"
