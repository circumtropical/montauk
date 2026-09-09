import pytest

from montauk.embeddings.local import LocalEmbeddingProvider
from montauk.models import Fact, Interaction, Person
from montauk.semantic_index import SemanticIndex, chunk_person


@pytest.fixture(scope="module")
def provider() -> LocalEmbeddingProvider:
    return LocalEmbeddingProvider()


def _mike_chen() -> Person:
    return Person(
        id="P0001",
        name="Mike Chen",
        summary="Robotics engineer met at a Stanford alumni event.",
        facts=[
            Fact(id="fact-1", category="Work & Education", text="Works in robotics at a startup."),
            Fact(id="fact-2", category="Family", text="Married with two kids."),
        ],
        interactions=[
            Interaction(id="int-1", date="2025", summary="Grabbed coffee and talked about robotics."),
            Interaction(id="int-2", date="2026-01"),  # no summary -> no chunk
        ],
    )


def _sarah_jones() -> Person:
    return Person(
        id="P0002",
        name="Sarah Jones",
        summary="Pastry chef who runs a bakery downtown.",
        facts=[Fact(id="fact-1", category="Interests", text="Bakes sourdough bread every weekend.")],
    )


def _both() -> dict[str, Person]:
    return {p.id: p for p in (_mike_chen(), _sarah_jones())}


def _index(tmp_path, provider) -> SemanticIndex:
    return SemanticIndex(tmp_path / "vectors", provider)


class TestChunkPerson:
    def test_chunks_summary_facts_and_interactions_with_summaries(self):
        chunk_ids = {c.chunk_id for c in chunk_person(_mike_chen())}
        assert chunk_ids == {"P0001:summary", "P0001:fact-1", "P0001:fact-2", "P0001:int-1"}
        # int-2 has no summary text, so it produces no chunk.

    def test_chunk_types_are_correct(self):
        chunks = {c.chunk_id: c for c in chunk_person(_mike_chen())}
        assert chunks["P0001:summary"].chunk_type == "summary"
        assert chunks["P0001:summary"].local_id is None
        assert chunks["P0001:fact-1"].chunk_type == "fact"
        assert chunks["P0001:fact-1"].local_id == "fact-1"
        assert chunks["P0001:int-1"].chunk_type == "interaction"
        assert chunks["P0001:int-1"].local_id == "int-1"

    def test_person_with_no_summary_or_facts_produces_no_chunks(self):
        assert chunk_person(Person(id="P0003", name="Empty Person")) == []


class TestRebuild:
    def test_rebuild_populates_expected_chunk_count(self, tmp_path, provider):
        index = _index(tmp_path, provider)
        index.rebuild(_both())
        # P0001: summary + 2 facts + 1 interaction-with-summary = 4; P0002: summary + 1 fact = 2
        assert index.chunk_count() == 6
        assert index.get_meta("dimension") == "384"

    def test_deleting_and_rebuilding_produces_equivalent_index(self, tmp_path, provider):
        index_dir = tmp_path / "vectors"
        index = SemanticIndex(index_dir, provider)
        index.rebuild(_both())
        chunks_before = sorted(
            (
                dict(r)
                for r in index._conn.execute(
                    "SELECT chunk_id, person_id, chunk_type, local_id, text FROM chunks"
                )
            ),
            key=lambda r: r["chunk_id"],
        )
        vectors_before = index._vectors.copy()
        index.close()

        (index_dir / "vectors.npy").unlink()
        (index_dir / "chunks.sqlite").unlink()

        index2 = SemanticIndex(index_dir, provider)
        index2.rebuild(_both())
        chunks_after = sorted(
            (
                dict(r)
                for r in index2._conn.execute(
                    "SELECT chunk_id, person_id, chunk_type, local_id, text FROM chunks"
                )
            ),
            key=lambda r: r["chunk_id"],
        )
        assert chunks_before == chunks_after
        assert vectors_before.shape == index2._vectors.shape


class TestRemoveAndUpsertPerson:
    def test_remove_person_drops_only_their_chunks_and_keeps_others_aligned(self, tmp_path, provider):
        index = _index(tmp_path, provider)
        index.rebuild(_both())

        index.remove_person("P0001")

        remaining = index._conn.execute("SELECT DISTINCT person_id FROM chunks").fetchall()
        assert {r["person_id"] for r in remaining} == {"P0002"}
        assert len(index._vectors) == index.chunk_count() == 2
        rows = index._conn.execute("SELECT row_index FROM chunks ORDER BY row_index").fetchall()
        assert [r["row_index"] for r in rows] == list(range(len(rows)))
        assert all(m.person_id == "P0002" for m in index.search("bakery pastry chef", limit=5))

    def test_upsert_person_replaces_their_chunks(self, tmp_path, provider):
        index = _index(tmp_path, provider)
        index.rebuild({"P0001": _mike_chen()})
        assert index.chunk_count() == 4

        index.upsert_person(
            _mike_chen().model_copy(update={"summary": "Now works in finance, not robotics."})
        )

        rows = index._conn.execute("SELECT text FROM chunks WHERE chunk_id = 'P0001:summary'").fetchall()
        assert rows[0]["text"] == "Now works in finance, not robotics."
        assert index.chunk_count() == 4  # same shape: summary + 2 facts + 1 interaction


class TestSearch:
    def test_returns_relevant_matches_ranked_by_score(self, tmp_path, provider):
        index = _index(tmp_path, provider)
        index.rebuild(_both())
        results = index.search("robotics engineer startup", limit=3)
        assert results and results[0].person_id == "P0001"

    def test_empty_index_returns_no_matches(self, tmp_path, provider):
        assert _index(tmp_path, provider).search("anything") == []

    def test_similarity_threshold_filters_weak_matches(self, tmp_path, provider):
        index = _index(tmp_path, provider)
        index.rebuild({"P0001": _mike_chen()})
        assert index.search("robotics engineer", similarity_threshold=0.99, limit=5) == []

    def test_limit_caps_result_count(self, tmp_path, provider):
        index = _index(tmp_path, provider)
        index.rebuild(_both())
        assert len(index.search("person", limit=1)) == 1
