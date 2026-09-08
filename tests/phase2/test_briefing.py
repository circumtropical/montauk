"""Progressive-disclosure agent retrieval (spec 16.2, 24.1, 24.2).

The normal pattern: a broad request gets a compact purpose-specific briefing
plus lightweight source refs; the agent drills down for evidence only when it
asks. Narrow deterministic questions skip the model entirely.
"""

from __future__ import annotations

import json

import pytest

from montauk.db import models as orm
from montauk.db.repositories import PeopleRepository, RevisionRepository
from montauk.llm.base import LLMResult
from montauk.llm.providers.fake import FakeProvider
from montauk.models import Fact, Interaction, Person
from montauk.services import briefing, model_config

PERSON = Person(
    id="P0001",
    name="Dana Whitfield",
    summary="College friend; robotics engineer.",
    birthday="1985-07-02",
    company="Nimbus Robotics",
    job_title="Staff engineer",
    location="Portland",
    facts=[
        Fact(
            id="fact-1",
            category="Work & Education",
            text="Promoted to staff engineer at Nimbus Robotics in January.",
        ),
        Fact(id="fact-2", category="Interests", text="Trains for trail-running ultramarathons on weekends."),
        Fact(id="fact-3", category="Family", text="Has a daughter, Robin, who is a junior in high school."),
        Fact(
            id="fact-4",
            category="Life Events",
            text="Bought a house in the Alberta Arts district last spring.",
        ),
        Fact(
            id="fact-5", category="General Notes", text="Recovering from a knee injury, cleared to run again."
        ),
    ],
    interactions=[
        Interaction(
            id="int-1",
            date="2024-02-01",
            channel="phone",
            summary="Caught up about the promotion and the knee.",
        ),
        Interaction(
            id="int-2",
            date="2024-05-20",
            channel="dinner",
            summary="Talked about Robin's college shortlist and campus visits.",
        ),
    ],
)


def _make_person(scope, person: Person = PERSON) -> orm.Person:
    row = PeopleRepository(scope).create(person)
    scope.session.flush()
    return row


def _configure_model(db_session, workspace, secret_box) -> None:
    model_config.save(
        db_session,
        workspace.id,
        "summarization",
        provider_type="claude_cli",
        model="claude-haiku-4-5",
        secret_box=secret_box,
    )
    db_session.flush()


def _fake(
    monkeypatch, text: str = "Dana was promoted to staff engineer.\nSOURCE_REFS: fact-1, int-1"
) -> FakeProvider:
    fake = FakeProvider(model="claude-haiku-4-5", responder=lambda s, p: text)
    monkeypatch.setattr(briefing, "build_provider", lambda cfg: fake)
    return fake


class _BoomProvider:
    provider_type = "fake"
    model = "claude-haiku-4-5"

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, **_kw) -> LLMResult:  # noqa: ANN003
        self.calls += 1
        raise AssertionError("the summarization provider must not be called in this mode")

    async def healthcheck(self) -> LLMResult:
        raise AssertionError("no healthcheck expected")


class TestProgressiveDisclosure:
    async def test_broad_request_returns_summary_without_the_full_evidence(
        self, db_session, scope, workspace, secret_box, monkeypatch
    ):
        _make_person(scope)
        _configure_model(db_session, workspace, secret_box)
        _fake(monkeypatch)

        result = await briefing.prepare_briefing(
            db_session,
            scope,
            public_id="P0001",
            purpose="brief me before I see Dana",
            secret_box=secret_box,
        )
        payload = result.agent_payload()

        assert result.generated is True
        assert payload["briefing"] == "Dana was promoted to staff engineer."
        assert payload["coverage"] == "standard"
        assert "evidence" not in payload  # not duplicated by default
        assert set(payload) <= {
            "person_id",
            "generated",
            "coverage",
            "source_refs",
            "mode",
            "briefing",
            "stale",
            "note",
        }

    async def test_source_refs_resolve_to_the_supporting_records(
        self, db_session, scope, workspace, secret_box, monkeypatch
    ):
        _make_person(scope)
        _configure_model(db_session, workspace, secret_box)
        _fake(monkeypatch, "Summary text.\nSOURCE_REFS: fact-1, int-2")

        result = await briefing.prepare_briefing(
            db_session,
            scope,
            public_id="P0001",
            purpose="catch me up on Dana",
            secret_box=secret_box,
        )
        assert result.source_refs == ["fact-1", "int-2"]

        sources = briefing.get_context_sources(scope, public_id="P0001", source_refs=result.source_refs)
        assert sources["missing_refs"] == []
        refs = {s["ref"]: s for s in sources["sources"]}
        assert refs["fact-1"]["text"].startswith("Promoted to staff engineer")
        assert refs["int-2"]["type"] == "interaction"

    async def test_narrow_structured_question_bypasses_the_model(
        self, db_session, scope, workspace, secret_box, monkeypatch
    ):
        _make_person(scope)
        _configure_model(db_session, workspace, secret_box)
        fake = _fake(monkeypatch)

        result = await briefing.prepare_briefing(
            db_session,
            scope,
            public_id="P0001",
            purpose="What is Dana's birthday?",
            secret_box=secret_box,
        )
        assert fake.calls == []
        assert result.generated is False and result.status == "direct"
        assert result.direct_field == "birthday"
        assert result.agent_payload()["answer"] == "1985-07-02"
        assert db_session.query(orm.LLMUsageEvent).count() == 0

    async def test_evidence_only_never_calls_the_provider(
        self, db_session, scope, workspace, secret_box, monkeypatch
    ):
        _make_person(scope)
        _configure_model(db_session, workspace, secret_box)
        boom = _BoomProvider()
        monkeypatch.setattr(briefing, "build_provider", lambda cfg: boom)

        result = await briefing.prepare_briefing(
            db_session,
            scope,
            public_id="P0001",
            purpose="brief me before I see Dana",
            mode="evidence_only",
            secret_box=secret_box,
        )
        assert boom.calls == 0
        assert result.generated is False and result.status == "ok"
        assert result.agent_payload()["evidence"]["facts"]
        assert "briefing" not in result.agent_payload()
        # nothing written to the summary cache for an evidence-only request
        assert db_session.query(orm.SummaryCacheEntry).count() == 0

    async def test_summary_with_evidence_returns_both_only_when_asked(
        self, db_session, scope, workspace, secret_box, monkeypatch
    ):
        _make_person(scope)
        _configure_model(db_session, workspace, secret_box)
        _fake(monkeypatch)

        default = await briefing.prepare_briefing(
            db_session,
            scope,
            public_id="P0001",
            purpose="brief me before I see Dana",
            secret_box=secret_box,
        )
        assert "evidence" not in default.agent_payload()

        both = await briefing.prepare_briefing(
            db_session,
            scope,
            public_id="P0001",
            purpose="brief me before I see Dana",
            mode="summary_with_evidence",
            secret_box=secret_box,
            force=True,
        )
        p = both.agent_payload()
        assert p["briefing"] and p["evidence"]["facts"]

    async def test_no_llm_returns_compact_evidence_with_unavailable_status(
        self, db_session, scope, secret_box
    ):
        _make_person(scope)
        result = await briefing.prepare_briefing(
            db_session,
            scope,
            public_id="P0001",
            purpose="brief me before I see Dana",
            secret_box=secret_box,
        )
        payload = result.agent_payload()
        assert result.generated is False
        assert payload["generated"] is False
        assert payload["status"] == "llm_unavailable"
        assert payload["evidence"]["facts"]
        assert "briefing" not in payload

    async def test_briefing_does_not_mutate_canonical_memory(
        self, db_session, scope, workspace, secret_box, monkeypatch
    ):
        row = _make_person(scope)
        _configure_model(db_session, workspace, secret_box)
        _fake(monkeypatch)

        before_facts = [(f.local_id, f.text) for f in row.facts]
        before_revisions = len(RevisionRepository(scope).for_person(row.id))

        await briefing.prepare_briefing(
            db_session,
            scope,
            public_id="P0001",
            purpose="brief me before I see Dana",
            secret_box=secret_box,
        )
        db_session.flush()
        db_session.refresh(row)

        assert [(f.local_id, f.text) for f in row.facts] == before_facts
        assert len(RevisionRepository(scope).for_person(row.id)) == before_revisions

    def test_source_refs_are_workspace_and_person_scoped(self, db_session, scope, other_scope):
        _make_person(scope)  # P0001 in workspace Alpha, with fact-1 and int-1
        _make_person(
            scope,
            Person(
                id="P0002",
                name="Other Person",
                facts=[Fact(id="fact-1", category="Interests", text="Sails.")],
            ),
        )
        db_session.flush()

        # Another workspace cannot reach this person's records at all.
        with pytest.raises(LookupError):
            briefing.get_context_sources(other_scope, public_id="P0001", source_refs=["fact-1"])

        # Refs resolve only against the named person's own record: P0001's
        # int-1 is not visible when drilling down on P0002.
        got = briefing.get_context_sources(
            scope, public_id="P0002", source_refs=["fact-1", "int-1", "fact-404"]
        )
        assert {s["ref"] for s in got["sources"]} == {"fact-1"}
        assert got["sources"][0]["text"] == "Sails."
        assert set(got["missing_refs"]) == {"int-1", "fact-404"}

    async def test_cached_briefing_goes_stale_after_a_relevant_change(
        self, db_session, scope, workspace, secret_box, monkeypatch
    ):
        row = _make_person(scope)
        _configure_model(db_session, workspace, secret_box)
        fake = _fake(monkeypatch)

        first = await briefing.prepare_briefing(
            db_session,
            scope,
            public_id="P0001",
            purpose="brief me before I see Dana",
            secret_box=secret_box,
        )
        assert first.generated and not first.cached

        cached = await briefing.prepare_briefing(
            db_session,
            scope,
            public_id="P0001",
            purpose="brief me before I see Dana",
            secret_box=secret_box,
        )
        assert cached.cached and not cached.stale
        assert len(fake.calls) == 1  # served from cache

        PeopleRepository(scope).add_fact(row, category="Life Events", text="Moved to Seattle for a new role.")
        db_session.flush()

        after = await briefing.prepare_briefing(
            db_session,
            scope,
            public_id="P0001",
            purpose="brief me before I see Dana",
            secret_box=secret_box,
        )
        assert after.cached and after.stale
        assert "changed" in (after.agent_payload().get("note") or "")

    async def test_default_response_is_materially_smaller_than_the_evidence(
        self, db_session, scope, workspace, secret_box, monkeypatch
    ):
        _make_person(scope)
        _configure_model(db_session, workspace, secret_box)
        _fake(
            monkeypatch,
            "Dana: promoted to staff engineer; training again after a knee injury; "
            "daughter Robin is touring colleges.\nSOURCE_REFS: fact-1, fact-3, fact-5, int-2",
        )

        result = await briefing.prepare_briefing(
            db_session,
            scope,
            public_id="P0001",
            purpose="brief me before I see Dana",
            secret_box=secret_box,
        )
        agent_bytes = len(json.dumps(result.agent_payload()))
        evidence_bytes = len(result.evidence_snapshot or "")

        assert evidence_bytes > 0
        assert agent_bytes < evidence_bytes / 2
