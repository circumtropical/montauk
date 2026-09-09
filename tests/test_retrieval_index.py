"""Semantic-index chunking, rebuild, staleness, and privacy.

The semantic index is not wired into any Phase 2 path yet (briefings and
the MCP ``prepare_person_context`` tool run lexical-only); these tests
keep the engine honest for a future revival.
"""

import datetime as dt

import pytest

from montauk.embeddings.local import LocalEmbeddingProvider
from montauk.models import Fact, Interaction, Person
from montauk.person_context import build_person_context
from montauk.semantic_index import SCHEMA_VERSION, SemanticIndex, chunk_person

LONG_SUMMARY = (
    "We met for the first date at the waterfront. We walked most of the pier and talked about "
    "her move from Denver and her nursing shifts. Then we climbed the old stone tower and she "
    "said she could stay up there all afternoon. Afterward we got coffee and she had to leave "
    "by six to let her dogs out. On the ferry back she asked whether I was really over my last "
    "relationship, which was a little pointed but fair. Overall it felt easy and low pressure "
    "even if I was not sure about the spark yet."
)


@pytest.fixture(scope="module")
def provider() -> LocalEmbeddingProvider:
    return LocalEmbeddingProvider()


def _person(**kw) -> Person:
    base = {"id": "P0001", "name": "Priya Raman", "summary": "A dating-app connection."}
    base.update(kw)
    return Person(**base)


class TestChunking:
    def test_each_semantic_unit_is_its_own_chunk(self):
        person = _person(
            facts=[
                Fact(id="fact-1", category="Interests", text="Climbs a lot."),
                Fact(id="fact-2", category="Family", text="Married to Sam.", related_person_id="P0002"),
            ],
            interactions=[Interaction(id="int-1", date="2026-08-20", summary="Short coffee.")],
        )
        chunks = {c.chunk_id: c for c in chunk_person(person)}
        assert set(chunks) == {"P0001:summary", "P0001:fact-1", "P0001:fact-2", "P0001:int-1"}
        assert chunks["P0001:summary"].chunk_type == "summary"
        assert chunks["P0001:fact-2"].chunk_type == "fact"  # relationship facts index as fact chunks

    def test_long_interaction_summary_is_split_at_sentence_boundaries(self):
        person = _person(interactions=[Interaction(id="int-1", date="2026-08-20", summary=LONG_SUMMARY)])
        chunks = [
            c
            for c in chunk_person(person, interaction_chunk_tokens=40, interaction_chunk_overlap_tokens=8)
            if c.chunk_type == "interaction"
        ]
        assert len(chunks) >= 3
        assert all(c.local_id == "int-1" for c in chunks)
        assert all(c.chunk_total == len(chunks) for c in chunks)
        assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
        for c in chunks:
            assert c.text.rstrip()[-1] in ".!?"
        assert any(chunks[i].text.split()[-3] in chunks[i + 1].text for i in range(len(chunks) - 1))

    def test_short_interaction_is_not_split(self):
        person = _person(
            interactions=[Interaction(id="int-1", date="2026-08-20", summary="Quick catch-up call.")]
        )
        chunks = [c for c in chunk_person(person) if c.chunk_type == "interaction"]
        assert len(chunks) == 1 and chunks[0].chunk_id == "P0001:int-1"

    def test_whole_person_is_never_one_chunk(self):
        person = _person(
            facts=[Fact(id=f"fact-{i}", category="Interests", text=f"thing {i}") for i in range(1, 6)]
        )
        assert len(chunk_person(person)) == 6  # summary + 5 facts, not 1


class TestRebuildAndStaleness:
    def test_rebuild_is_idempotent(self, tmp_path, provider):
        people = {"P0001": _person(facts=[Fact(id="fact-1", category="Interests", text="Climbs.")])}
        idx = SemanticIndex(tmp_path / "vectors", provider)
        idx.rebuild(people)
        first = sorted(r["chunk_id"] for r in idx._conn.execute("SELECT chunk_id FROM chunks"))
        idx.rebuild(people)
        second = sorted(r["chunk_id"] for r in idx._conn.execute("SELECT chunk_id FROM chunks"))
        assert first == second
        assert idx.get_meta("schema_version") == SCHEMA_VERSION

    def test_chunking_config_change_marks_index_stale(self, tmp_path, provider):
        people = {"P0001": _person(interactions=[Interaction(id="int-1", date="2026", summary=LONG_SUMMARY)])}
        vdir = tmp_path / "vectors"
        SemanticIndex(vdir, provider, interaction_chunk_tokens=120).rebuild(people)
        reopened = SemanticIndex(vdir, provider, interaction_chunk_tokens=40)
        assert reopened.stale_reason() is not None
        assert "chunk" in reopened.stale_reason()

    def test_content_hashes_are_recorded_for_drift_detection(self, tmp_path, provider):
        idx = SemanticIndex(tmp_path / "vectors", provider)
        idx.rebuild({"P0001": _person()}, content_hashes={"P0001": "abc123"})
        assert idx.person_content_hash("P0001") == "abc123"


class TestLexicalFallback:
    def test_build_person_context_works_with_no_index(self):
        person = _person(facts=[Fact(id="fact-1", category="Interests", text="Climbs at the gym.")])
        r = build_person_context(
            person,
            "where does Priya climb",
            detail_level="standard",
            budget_tokens=2000,
            semantic_index=None,
            now=dt.date(2026, 9, 5),
        )
        payload = r.to_payload()
        assert payload["retrieval"]["semantic_available"] is False
        assert payload["retrieval"]["note"]  # says lexical-only
        assert any("gym" in f["text"] for f in payload["facts"])


class TestPrivacy:
    def test_default_embedding_provider_is_local_only(self):
        assert LocalEmbeddingProvider.provider == "local"

    def test_index_state_carries_no_personal_content(self, tmp_path, provider):
        secret = "Priya confided something very private about her family."
        idx = SemanticIndex(tmp_path / "vectors", provider)
        idx.rebuild(
            {
                "P0001": _person(
                    summary=secret, facts=[Fact(id="fact-1", category="General Notes", text=secret)]
                )
            }
        )
        assert secret not in repr(idx.index_state(active_person_ids={"P0001"}))
