"""Semantic-index chunking (pure). Storage, reconciliation, and search
run against PostgreSQL in tests/phase2/test_semantic.py."""

from montauk.models import Fact, Interaction, Person
from montauk.semantic_index import chunk_person


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
