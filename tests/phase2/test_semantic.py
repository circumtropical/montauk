"""Semantic index over PostgreSQL: reconcile-on-read, search, isolation,
fingerprint drift, and lexical degradation (spec 19, 20; ADR 0005)."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from montauk.db import models as orm
from montauk.db.repositories import PeopleRepository
from montauk.embeddings.local import LocalEmbeddingProvider
from montauk.models import Fact, Interaction, Person
from montauk.semantic_index import SemanticIndex


@pytest.fixture(scope="module")
def local_provider() -> LocalEmbeddingProvider:
    return LocalEmbeddingProvider()


class CountingProvider:
    """Wraps the real local model and records every text it embeds."""

    def __init__(self, inner: LocalEmbeddingProvider, *, model_name: str | None = None):
        self.inner = inner
        self.model_name = model_name or inner.model_name
        self.embedded: list[str] = []

    @property
    def dimension(self) -> int:
        return self.inner.dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.embedded.extend(texts)
        return self.inner.embed(texts)


class FailingProvider:
    model_name = "broken"
    dimension = 384

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("model unavailable")


@pytest.fixture
def provider(local_provider) -> CountingProvider:
    return CountingProvider(local_provider)


def _mike_chen(pid: str = "P0001") -> Person:
    return Person(
        id=pid,
        name="Mike Chen",
        summary="Met at an MIT alumni mixer last spring.",
        facts=[
            Fact(id="fact-1", category="Work & Education", text="Builds warehouse robots at a startup."),
            Fact(id="fact-2", category="Family", text="Married with two kids."),
        ],
        interactions=[
            Interaction(id="int-1", date="2025", summary="Grabbed coffee and talked about robotics."),
            Interaction(id="int-2", date="2026-01"),  # no summary -> no chunk
        ],
    )


def _sarah_jones(pid: str = "P0002") -> Person:
    return Person(
        id=pid,
        name="Sarah Jones",
        summary="Pastry chef who runs a bakery downtown.",
        facts=[Fact(id="fact-1", category="Interests", text="Bakes sourdough bread every weekend.")],
    )


def _seed(scope, *people: Person) -> None:
    repo = PeopleRepository(scope)
    for p in people:
        repo.create(p)
    scope.session.flush()


def _chunk_ids(session, workspace_id) -> list[str]:
    return sorted(
        session.execute(
            select(orm.SemanticChunk.chunk_id).where(orm.SemanticChunk.workspace_id == workspace_id)
        ).scalars()
    )


class TestReconcile:
    def test_first_search_embeds_every_active_person_once(self, scope, provider):
        _seed(scope, _mike_chen(), _sarah_jones())
        index = SemanticIndex(scope, provider)
        index.search("robots")
        # P0001: summary + 2 facts + 1 interaction-with-summary; P0002: summary + 1 fact
        assert _chunk_ids(scope.session, scope.workspace_id) == [
            "P0001:fact-1",
            "P0001:fact-2",
            "P0001:int-1",
            "P0001:summary",
            "P0002:fact-1",
            "P0002:summary",
        ]
        embedded = len(provider.embedded)
        index.search("bread")
        assert len(provider.embedded) == embedded + 1  # only the query; nothing re-embedded

    def test_an_edit_re_embeds_only_that_person(self, scope, provider):
        _seed(scope, _mike_chen(), _sarah_jones())
        index = SemanticIndex(scope, provider)
        assert index.sync_workspace() == 2
        repo = PeopleRepository(scope)
        repo.update_fact(repo.require("P0002"), "fact-1", text="Bakes croissants for the farmers market.")
        scope.session.flush()
        provider.embedded.clear()

        assert index.sync_workspace() == 1
        assert set(provider.embedded) == {
            "Pastry chef who runs a bakery downtown.",
            "Bakes croissants for the farmers market.",
        }
        hits = index.search("farmers market pastries", limit=1)
        assert hits[0].person_id == "P0002" and "croissants" in hits[0].text

    def test_structured_field_edit_does_not_re_embed(self, scope, provider):
        _seed(scope, _mike_chen())
        index = SemanticIndex(scope, provider)
        index.sync_workspace()
        repo = PeopleRepository(scope)
        repo.update_core_fields(repo.require("P0001"), _mike_chen().model_copy(update={"company": "Acme"}))
        scope.session.flush()
        assert index.sync_workspace() == 0

    def test_model_change_re_embeds_everyone(self, scope, local_provider):
        _seed(scope, _mike_chen(), _sarah_jones())
        SemanticIndex(scope, CountingProvider(local_provider)).sync_workspace()
        renamed = CountingProvider(local_provider, model_name="some/other-model")
        index = SemanticIndex(scope, renamed)
        assert index.status()["stale_person_ids"] == ["P0001", "P0002"]
        assert index.sync_workspace() == 2

    def test_chunking_change_re_embeds(self, scope, provider):
        _seed(scope, _mike_chen())
        SemanticIndex(scope, provider).sync_workspace()
        assert SemanticIndex(scope, provider, interaction_chunk_tokens=40).sync_workspace() == 1

    def test_rebuild_is_equivalent(self, scope, provider):
        _seed(scope, _mike_chen(), _sarah_jones())
        index = SemanticIndex(scope, provider)
        index.sync_workspace()
        before = _chunk_ids(scope.session, scope.workspace_id)
        assert index.rebuild() == 2
        assert _chunk_ids(scope.session, scope.workspace_id) == before
        assert index.status()["stale_person_ids"] == []

    def test_provider_failure_leaves_the_session_usable(self, scope):
        _seed(scope, _mike_chen())
        with pytest.raises(RuntimeError):
            SemanticIndex(scope, FailingProvider()).search("robots")
        assert PeopleRepository(scope).require("P0001").name == "Mike Chen"


class TestSearch:
    def test_vague_description_finds_the_person(self, scope, provider):
        _seed(scope, _mike_chen(), _sarah_jones())
        hits = SemanticIndex(scope, provider).search(
            "the robotics guy from the MIT mixer", limit=3, similarity_threshold=0.35
        )
        assert hits and hits[0].person_id == "P0001"

    def test_threshold_and_limit(self, scope, provider):
        _seed(scope, _mike_chen(), _sarah_jones())
        index = SemanticIndex(scope, provider)
        assert index.search("robotics engineer", similarity_threshold=0.99) == []
        assert len(index.search("person", limit=1)) == 1

    def test_archived_people_are_excluded_from_workspace_search(self, scope, provider):
        _seed(scope, _mike_chen(), _sarah_jones())
        repo = PeopleRepository(scope)
        repo.set_archived(repo.require("P0002"), True)
        scope.session.flush()
        index = SemanticIndex(scope, provider)
        assert all(m.person_id == "P0001" for m in index.search("bakery pastry chef", limit=10))
        # ...but an archived person's own record is still searchable once resolved
        assert index.search_person("bakery", "P0002")[0].person_id == "P0002"

    def test_search_person_never_crosses_people(self, scope, provider):
        _seed(scope, _mike_chen(), _sarah_jones())
        hits = SemanticIndex(scope, provider).search_person("sourdough bread", "P0001", limit=10)
        assert hits and all(m.person_id == "P0001" for m in hits)

    def test_workspaces_are_isolated(self, scope, other_scope, provider):
        _seed(scope, _mike_chen())
        _seed(other_scope, _sarah_jones(pid="P0001"))
        hits = SemanticIndex(other_scope, provider).search("robots", limit=10)
        assert all("robot" not in m.text for m in hits)
        assert all(m.person_id == "P0001" for m in hits)
        assert SemanticIndex(scope, provider).status()["indexed_people"] == 0  # untouched


class TestPrivacy:
    def test_status_carries_no_personal_content(self, scope, provider):
        secret = "Confided something very private about their family."
        _seed(scope, _mike_chen().model_copy(update={"summary": secret}))
        index = SemanticIndex(scope, provider)
        index.sync_workspace()
        assert secret not in repr(index.status())


class TestCli:
    def test_rebuild_then_status(self, database_url, db_session, scope):
        from typer.testing import CliRunner

        from montauk.cli import app

        _seed(scope, _mike_chen(), _sarah_jones())
        db_session.commit()
        runner = CliRunner()
        out = runner.invoke(app, ["semantic", "rebuild", "--database-url", database_url])
        assert out.exit_code == 0, out.output
        assert "re-embedded 2 people" in out.output
        out = runner.invoke(app, ["semantic", "status", "--database-url", database_url])
        assert out.exit_code == 0, out.output
        assert "2/2 people indexed, 6 chunks" in out.output and "stale: none" in out.output
