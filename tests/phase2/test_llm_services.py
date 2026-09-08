"""Model config, usage/limits, and on-demand summaries (spec 16, 24.2)."""

from __future__ import annotations

import pytest

from montauk.db import models as orm
from montauk.db.repositories import PeopleRepository
from montauk.llm.base import LLMError, LLMRateLimited
from montauk.llm.providers.fake import FakeProvider
from montauk.models import Fact, Interaction, Person
from montauk.services import llm_usage, model_config, summaries


def _make_person(scope, pid="P0001", name="Dana Whitfield"):
    repo = PeopleRepository(scope)
    row = repo.create(
        Person(
            id=pid,
            name=name,
            summary="College friend; robotics engineer.",
            facts=[
                Fact(id="fact-1", category="Work & Education", text="Staff engineer at Nimbus Robotics."),
                Fact(id="fact-2", category="Interests", text="Trail running and homemade pasta."),
            ],
            interactions=[
                Interaction(
                    id="int-1", date="2024-02-01", channel="phone", summary="Caught up about the new job."
                ),
            ],
        )
    )
    scope.session.flush()
    return row


class TestModelConfig:
    def test_save_and_resolve_round_trip_with_secret(self, db_session, workspace, secret_box):
        model_config.save(
            db_session,
            workspace.id,
            "summarization",
            provider_type="anthropic_api",
            model="claude-haiku-4-5",
            api_key="sk-ant-secret",
            price_input="1.0",
            price_output="5.0",
            secret_box=secret_box,
        )
        db_session.flush()

        row = model_config.get_row(db_session, workspace.id, "summarization")
        assert row.api_key_ciphertext and "sk-ant-secret" not in row.api_key_ciphertext

        resolved = model_config.resolve(db_session, workspace.id, "summarization", secret_box=secret_box)
        assert resolved.api_key == "sk-ant-secret"
        assert resolved.price_override == (1.0, 5.0)

        v = model_config.view(db_session, workspace.id, "summarization")
        assert v.has_api_key is True and v.model == "claude-haiku-4-5"

    def test_keeps_existing_key_when_field_left_blank(self, db_session, workspace, secret_box):
        model_config.save(
            db_session,
            workspace.id,
            "summarization",
            provider_type="anthropic_api",
            model="m",
            api_key="k1",
            secret_box=secret_box,
        )
        db_session.flush()
        model_config.save(
            db_session,
            workspace.id,
            "summarization",
            provider_type="anthropic_api",
            model="m2",
            api_key="",
            secret_box=secret_box,
        )
        db_session.flush()
        r = model_config.resolve(db_session, workspace.id, "summarization", secret_box=secret_box)
        assert r.model == "m2" and r.api_key == "k1"

    def test_cli_provider_needs_no_key(self, db_session, workspace, secret_box):
        model_config.save(
            db_session,
            workspace.id,
            "summarization",
            provider_type="claude_cli",
            model="claude-haiku-4-5",
            secret_box=secret_box,
        )
        db_session.flush()
        r = model_config.resolve(db_session, workspace.id, "summarization", secret_box=secret_box)
        assert r.provider_type == "claude_cli" and r.api_key is None

    def test_validation(self, db_session, workspace, secret_box):
        with pytest.raises(model_config.ModelConfigError):
            model_config.save(
                db_session,
                workspace.id,
                "summarization",
                provider_type="openai_compatible",
                model="m",
                base_url="",
                secret_box=secret_box,
            )
        with pytest.raises(model_config.ModelConfigError):
            model_config.save(
                db_session,
                workspace.id,
                "summarization",
                provider_type="anthropic_api",
                model="m",
                api_key="",
                secret_box=secret_box,
            )

    def test_separate_purposes(self, db_session, workspace, secret_box):
        model_config.save(
            db_session,
            workspace.id,
            "summarization",
            provider_type="claude_cli",
            model="claude-opus-5",
            secret_box=secret_box,
        )
        model_config.save(
            db_session,
            workspace.id,
            "extraction",
            provider_type="anthropic_api",
            model="claude-haiku-4-5",
            api_key="k",
            secret_box=secret_box,
        )
        db_session.flush()
        assert (
            model_config.resolve(db_session, workspace.id, "summarization", secret_box=secret_box).model
            == "claude-opus-5"
        )
        assert (
            model_config.resolve(db_session, workspace.id, "extraction", secret_box=secret_box).model
            == "claude-haiku-4-5"
        )


class TestUsageAndBudget:
    def _settings(self, db_session, workspace):
        s = db_session.get(orm.WorkspaceSettings, workspace.id)
        if s is None:
            s = orm.WorkspaceSettings(workspace_id=workspace.id)
            db_session.add(s)
            db_session.flush()
        return s

    def test_month_to_date_and_limits(self, db_session, workspace):
        settings = self._settings(db_session, workspace)
        for _ in range(3):
            llm_usage.record(db_session, workspace.id, purpose="summarization", result=_fake_result())
        db_session.flush()
        totals = llm_usage.month_to_date(db_session, workspace.id)
        assert totals.calls == 3 and totals.input_tokens > 0

        settings.monthly_token_limit = 1
        st = llm_usage.budget_state(db_session, workspace.id, settings)
        assert st.over_token_limit and st.blocked
        with pytest.raises(LLMError):
            llm_usage.ensure_within_budget(db_session, workspace.id, settings)

    def test_pause_switch_blocks(self, db_session, workspace):
        settings = self._settings(db_session, workspace)
        settings.llm_processing_paused = True
        assert llm_usage.budget_state(db_session, workspace.id, settings).blocked

    def test_failed_call_recorded_with_category(self, db_session, workspace):
        llm_usage.record(db_session, workspace.id, purpose="summarization", error=LLMRateLimited("slow"))
        db_session.flush()
        ev = db_session.query(orm.LLMUsageEvent).one()
        assert ev.ok is False and ev.error_category == "rate_limit"


def _fake_result():
    from montauk.llm.base import LLMResult

    return LLMResult(
        text="x", input_tokens=100, output_tokens=20, model="claude-haiku-4-5", provider_type="fake"
    )


class TestSummaries:
    async def test_no_llm_returns_deterministic_evidence(self, db_session, scope, secret_box):
        _make_person(scope)
        r = await summaries.generate_summary(
            db_session,
            scope,
            public_id="P0001",
            purpose="what does she do for work",
            secret_box=secret_box,
        )
        assert r.generated is False and r.status == "llm_unavailable"
        assert "Nimbus Robotics" in r.body  # deterministic evidence is included
        assert r.evidence["facts"]

    async def test_generates_and_caches_with_a_configured_model(
        self, db_session, scope, workspace, secret_box, monkeypatch
    ):
        _make_person(scope)
        model_config.save(
            db_session,
            workspace.id,
            "summarization",
            provider_type="claude_cli",
            model="claude-haiku-4-5",
            secret_box=secret_box,
        )
        db_session.flush()

        fake = FakeProvider(model="claude-haiku-4-5", responder=lambda s, p: "Dana is a robotics engineer.")
        monkeypatch.setattr(summaries, "build_provider", lambda cfg: fake)

        r1 = await summaries.generate_summary(
            db_session, scope, public_id="P0001", purpose="work summary", secret_box=secret_box
        )
        assert r1.generated and r1.status == "ok" and r1.body == "Dana is a robotics engineer."
        assert len(fake.calls) == 1

        # second call is served from cache -- provider not hit again
        r2 = await summaries.generate_summary(
            db_session, scope, public_id="P0001", purpose="work summary", secret_box=secret_box
        )
        assert r2.cached and r2.body == r1.body
        assert len(fake.calls) == 1

        # usage recorded once
        assert db_session.query(orm.LLMUsageEvent).filter_by(ok=True).count() == 1

    async def test_editing_the_person_marks_the_cached_summary_stale(
        self, db_session, scope, workspace, secret_box, monkeypatch
    ):
        row = _make_person(scope)
        model_config.save(
            db_session,
            workspace.id,
            "summarization",
            provider_type="claude_cli",
            model="m",
            secret_box=secret_box,
        )
        db_session.flush()
        monkeypatch.setattr(summaries, "build_provider", lambda cfg: FakeProvider())

        await summaries.generate_summary(
            db_session, scope, public_id="P0001", purpose="p", secret_box=secret_box
        )
        PeopleRepository(scope).add_fact(row, category="Life Events", text="Moved to Boston.")
        db_session.flush()

        r = await summaries.generate_summary(
            db_session, scope, public_id="P0001", purpose="p", secret_box=secret_box
        )
        assert r.cached and r.stale and "changed" in (r.note or "")

        r2 = await summaries.generate_summary(
            db_session, scope, public_id="P0001", purpose="p", secret_box=secret_box, force=True
        )
        assert not r2.cached and not r2.stale

    async def test_budget_block_falls_back_to_deterministic(
        self, db_session, scope, workspace, secret_box, monkeypatch
    ):
        _make_person(scope)
        model_config.save(
            db_session,
            workspace.id,
            "summarization",
            provider_type="claude_cli",
            model="m",
            secret_box=secret_box,
        )
        settings = db_session.get(orm.WorkspaceSettings, workspace.id)
        settings.llm_processing_paused = True
        db_session.flush()
        monkeypatch.setattr(summaries, "build_provider", lambda cfg: FakeProvider())

        r = await summaries.generate_summary(
            db_session, scope, public_id="P0001", purpose="p", secret_box=secret_box
        )
        assert r.generated is False and r.status == "budget_exceeded"
        assert "paused" in (r.note or "").lower()

    async def test_provider_error_falls_back_and_records_failure(
        self, db_session, scope, workspace, secret_box, monkeypatch
    ):
        _make_person(scope)
        model_config.save(
            db_session,
            workspace.id,
            "summarization",
            provider_type="claude_cli",
            model="m",
            secret_box=secret_box,
        )
        db_session.flush()
        monkeypatch.setattr(
            summaries,
            "build_provider",
            lambda cfg: FakeProvider(fail_with=LLMRateLimited("try later")),
        )
        r = await summaries.generate_summary(
            db_session, scope, public_id="P0001", purpose="p", secret_box=secret_box
        )
        assert r.generated is False and r.status == "llm_error"
        assert db_session.query(orm.LLMUsageEvent).filter_by(ok=False).count() == 1

    async def test_rejects_blank_purpose(self, db_session, scope, secret_box):
        _make_person(scope)
        with pytest.raises(ValueError):
            await summaries.generate_summary(
                db_session, scope, public_id="P0001", purpose="   ", secret_box=secret_box
            )
