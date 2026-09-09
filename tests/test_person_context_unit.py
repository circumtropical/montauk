"""Unit tests for the pure retrieval/ranking/serialization logic in
person_context.py -- no MCP, no embeddings."""

import datetime as dt

import pytest
from _people import priya as _priya

from montauk.models import Fact, Interaction, Person
from montauk.person_context import (
    ContextUnit,
    analyze_purpose,
    build_person_context,
    build_units,
    dedupe,
    lexical_scores,
)

NOW = dt.date(2026, 9, 5)


@pytest.fixture(scope="module")
def priya() -> Person:
    return _priya()


class TestAnalyzePurpose:
    def test_strips_subject_name_and_stopwords(self):
        a = analyze_purpose("What was Nicole's dog's name?", subject_name="Nicole Cupples")
        assert "nicole" not in {t.lower() for t in a.terms}
        assert {t.lower() for t in a.terms} == {"dog", "name"}

    def test_detects_temporal_purpose(self):
        assert analyze_purpose("Have I waited too long to reach out again?").is_temporal
        assert not analyze_purpose("What are her hobbies?").is_temporal

    def test_detects_event_purpose(self):
        assert analyze_purpose("What did she say on the hike?").wants_events

    def test_detects_briefing(self):
        assert analyze_purpose("Give me a complete briefing on her.").is_briefing
        assert analyze_purpose("Remind me who she is and how I know her").is_briefing

    def test_extracts_quoted_phrase(self):
        a = analyze_purpose('did she say "stay up here all afternoon"?')
        assert a.quoted_phrases == ("stay up here all afternoon",)

    def test_possessive_is_normalized(self):
        a = analyze_purpose("What is Priya's job?", subject_name="Priya Raman")
        assert "job" in {t.lower() for t in a.terms}


class TestBuildUnits:
    def test_summary_facts_relationships_interactions(self, priya):
        units = build_units(priya)
        kinds = {u.kind for u in units}
        assert kinds == {"summary", "fact", "relationship", "interaction"}
        # fact-2 has related_person_id -> relationship
        rel = [u for u in units if u.kind == "relationship"]
        assert [u.record_id for u in rel] == ["fact-2"]
        assert rel[0].related_person_id == "P0002"

    def test_text_is_verbatim_whitespace_normalized(self, priya):
        units = {u.record_id: u for u in build_units(priya)}
        assert units["fact-4"].text == "Priya said she likes sour beers and cannot stand IPAs."
        assert "  " not in units["fact-8"].text
        assert '"I could stay up here all afternoon."' in units["fact-8"].text

    def test_person_with_nothing_yields_no_units(self):
        assert build_units(Person(id="P0009", name="Nobody")) == []


class TestLexicalScores:
    def _units(self):
        return [
            ContextUnit(kind="fact", record_id="fact-1", text="She has two rescue dogs at home."),
            ContextUnit(kind="fact", record_id="fact-2", text="She is a nurse at the hospital."),
            ContextUnit(kind="fact", record_id="fact-3", text="She likes sour beer and climbing."),
        ]

    def test_matches_relevant_units_only(self):
        scores = lexical_scores(self._units(), analyze_purpose("what about her dogs"))
        assert "fact-1" in scores
        assert "fact-2" not in scores

    def test_no_query_no_scores(self):
        assert lexical_scores(self._units(), analyze_purpose("")) == {}

    def test_scores_are_bounded(self):
        scores = lexical_scores(self._units(), analyze_purpose("nurse hospital beer climbing dogs"))
        assert all(0 < v <= 1.0 for v in scores.values())


class TestDedupe:
    def test_drops_exact_duplicates(self):
        units = [
            ContextUnit(kind="fact", record_id="fact-1", text="She has two dogs.", score=0.9),
            ContextUnit(kind="fact", record_id="fact-2", text="she has two dogs.", score=0.5),
        ]
        assert [u.record_id for u in dedupe(units)] == ["fact-1"]

    def test_keeps_distinct_evidence(self):
        units = [
            ContextUnit(
                kind="fact", record_id="fact-1", text="She moved here in 2025 after a breakup.", score=0.9
            ),
            ContextUnit(
                kind="interaction",
                record_id="int-1",
                text="We talked about her recent move and her dogs.",
                score=0.8,
            ),
        ]
        assert len(dedupe(units)) == 2

    def test_collapses_near_duplicate_facts_without_merging_text(self):
        units = [
            ContextUnit(
                kind="fact",
                record_id="fact-1",
                text="Priya likes sour beers and cannot stand IPAs.",
                score=0.9,
            ),
            ContextUnit(
                kind="fact",
                record_id="fact-2",
                text="Priya likes sour beers and cannot stand IPAs really.",
                score=0.4,
            ),
        ]
        kept = dedupe(units)
        assert len(kept) == 1
        assert kept[0].text == "Priya likes sour beers and cannot stand IPAs."  # unchanged, not merged


class TestBudgetAndSelection:
    def test_respects_token_budget(self, priya):
        r = build_person_context(
            priya, "Tell me everything about Priya", detail_level="comprehensive", budget_tokens=200, now=NOW
        )
        assert r.approximate_tokens <= 200 or r.returned_items == 1
        assert r.truncated is True
        assert r.additional_matching_items > 0

    def test_explicit_budget_overrides_preset(self, priya):
        small = build_person_context(
            priya, "briefing on Priya", detail_level="comprehensive", budget_tokens=120, now=NOW
        )
        big = build_person_context(
            priya, "briefing on Priya", detail_level="comprehensive", budget_tokens=6000, now=NOW
        )
        assert small.returned_items < big.returned_items

    def test_narrow_lookup_is_compact(self, priya):
        r = build_person_context(
            priya, "What was Priya's dog's name?", detail_level="standard", budget_tokens=2000, now=NOW
        )
        assert r.returned_items <= 4
        ids = [u.record_id for u in r.facts]
        assert "fact-5" in ids  # the dog fact
        # ...and no dog name anywhere in the returned evidence
        blob = " ".join(u.text for u in r.facts + r.interactions).lower()
        assert "dog" in blob

    def test_narrow_lookup_omits_summary_when_summary_not_a_match(self, priya):
        r = build_person_context(
            priya, "What was Priya's dog's name?", detail_level="standard", budget_tokens=2000, now=NOW
        )
        assert r.summary is None

    def test_identity_purpose_includes_summary(self, priya):
        r = build_person_context(
            priya,
            "Remind me who Priya is and how I know her",
            detail_level="standard",
            budget_tokens=2000,
            now=NOW,
        )
        assert r.summary is not None


class TestTemporal:
    def test_temporal_block_only_for_temporal_or_comprehensive(self, priya):
        assert (
            build_person_context(
                priya, "What are her interests?", detail_level="standard", budget_tokens=2000, now=NOW
            ).temporal
            == {}
        )
        t = build_person_context(
            priya, "How long since I last saw Priya?", detail_level="standard", budget_tokens=2000, now=NOW
        ).temporal
        assert t["last_recorded_interaction"] == "2026-08-27"
        assert t["days_since_last_recorded_interaction"] == 9
        assert t["supporting_interaction_id"] == "int-3"

    def test_no_elapsed_days_for_partial_date(self):
        person = Person(
            id="P0001",
            name="X",
            interactions=[Interaction(id="int-1", date="2026", summary="met once")],
        )
        t = build_person_context(
            person, "how long since we spoke", detail_level="standard", budget_tokens=2000, now=NOW
        ).temporal
        assert t["days_since_last_recorded_interaction"] is None
        assert "not full-precision" in t["note"]

    def test_no_subjective_conclusions(self, priya):
        t = build_person_context(
            priya,
            "have I waited too long to reach out?",
            detail_level="standard",
            budget_tokens=2000,
            now=NOW,
        ).temporal
        assert "waited_too_long" not in t
        assert not any(isinstance(v, bool) for v in t.values())


class TestSerialization:
    def test_omits_default_high_confidence_includes_low(self, priya):
        r = build_person_context(
            priya, "is she learning any instruments", detail_level="standard", budget_tokens=2000, now=NOW
        )
        payload = r.to_payload()
        by_id = {f["id"]: f for f in payload["facts"]}
        assert "fact-14" in by_id  # banjo, confidence: low
        assert by_id["fact-14"]["confidence"] == "low"
        for f in payload["facts"]:
            assert f.get("confidence") != "high"

    def test_no_storage_metadata_in_payload(self, priya):
        payload = build_person_context(
            priya, "briefing", detail_level="comprehensive", budget_tokens=6000, now=NOW
        ).to_payload()
        blob = repr(payload)
        for banned in (
            "content_hash",
            "embedding",
            "chunk_id",
            "row_index",
            "file_path",
            ".md",
            "created_at",
            "updated_at",
        ):
            assert banned not in blob
        for f in payload["facts"]:
            assert set(f) <= {"id", "text", "section", "date", "confidence", "related_person_id"}
        assert "id" in payload["person"] and "name" in payload["person"]

    def test_relationship_surfaces_related_person_id(self, priya):
        payload = build_person_context(
            priya, "who introduced us / how did we meet", detail_level="standard", budget_tokens=2000, now=NOW
        ).to_payload()
        rels = {r["id"]: r for r in payload["relationships"]}
        assert "fact-2" in rels
        assert rels["fact-2"]["related_person_id"] == "P0002"


class TestNoSemantic:
    def test_lexical_only_when_no_index(self, priya):
        r = build_person_context(
            priya,
            "What did Priya say at the top of the tower?",
            detail_level="standard",
            budget_tokens=2000,
            semantic_index=None,
            now=NOW,
        )
        assert r.semantic_available is False
        assert r.semantic_note
        ids = [u.record_id for u in r.facts]
        assert "fact-8" in ids  # the tower quote fact, found lexically

    def test_lexical_only_still_answers_exact_queries(self):
        person = Person(
            id="P0001",
            name="Priya Raman",
            facts=[
                Fact(id="fact-1", category="Interests", text="Priya plays the banjo."),
                Fact(id="fact-2", category="Work & Education", text="Priya is a nurse."),
            ],
        )
        r = build_person_context(
            person,
            "does Priya play any instruments?",
            detail_level="standard",
            budget_tokens=2000,
            semantic_index=None,
            now=NOW,
        )
        assert [u.record_id for u in r.facts] == ["fact-1"]
