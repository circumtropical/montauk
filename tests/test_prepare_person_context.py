"""prepare_person_context MCP tool: contract, budgets, metadata rules,
privacy, index sync, lexical fallback, and the representative dating-
contact question scenarios (spec amendment section 13)."""

import datetime as dt
import shutil
from pathlib import Path

import pytest
from _helpers import call, call_expecting_error, running_session

from montauk.bootstrap import reconcile_on_startup
from montauk.embeddings.local import LocalEmbeddingProvider

DATING_FIXTURE = Path(__file__).parent.parent / "examples" / "dating"


@pytest.fixture(scope="module")
def provider() -> LocalEmbeddingProvider:
    return LocalEmbeddingProvider()


def _seed(ctx) -> None:
    for md in (DATING_FIXTURE / "people").glob("*.md"):
        shutil.copy(md, ctx.store.people_dir / md.name)
    reconcile_on_startup(ctx)


async def _ctx_call(session, person_id="P0001", purpose="Tell me about Priya", **kw):
    return await call(session, "prepare_person_context", person_id=person_id, purpose=purpose, **kw)


# --------------------------------------------------------------------------
# tool contract + validation
# --------------------------------------------------------------------------


class TestContract:
    @pytest.mark.asyncio
    async def test_blank_purpose_rejected(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            _seed(_ctx)
            text = await call_expecting_error(session, "prepare_person_context", person_id="P0001", purpose="   ")
            assert "VALIDATION_ERROR" in text

    @pytest.mark.asyncio
    async def test_unknown_person_is_not_found(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            _seed(_ctx)
            text = await call_expecting_error(session, "prepare_person_context", person_id="P0404", purpose="who?")
            assert "NOT_FOUND" in text

    @pytest.mark.asyncio
    async def test_name_derived_person_id_rejected(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            _seed(_ctx)
            text = await call_expecting_error(
                session, "prepare_person_context", person_id="priya-raman", purpose="who?"
            )
            assert "NOT_FOUND" in text

    @pytest.mark.asyncio
    async def test_invalid_detail_level_rejected(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            _seed(_ctx)
            text = await call_expecting_error(
                session, "prepare_person_context", person_id="P0001", purpose="who?", detail_level="everything"
            )
            assert "VALIDATION_ERROR" in text

    @pytest.mark.asyncio
    async def test_unsafe_token_budget_rejected(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            _seed(_ctx)
            for bad in (5, 999999):
                text = await call_expecting_error(
                    session, "prepare_person_context", person_id="P0001", purpose="who?", max_tokens=bad
                )
                assert "VALIDATION_ERROR" in text

    @pytest.mark.asyncio
    async def test_response_shape(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            _seed(_ctx)
            r = await _ctx_call(session, purpose="Remind me who Priya is and how I know her")
            assert r["person"] == {"id": "P0001", "name": "Priya Raman"}
            assert r["detail_level"] == "standard"
            assert set(r["retrieval"]) >= {
                "semantic_available", "truncated", "returned_items",
                "additional_matching_items", "approximate_tokens",
            }
            for section in ("facts", "relationships", "interactions"):
                for item in r[section]:
                    assert "id" in item and "text" in item


class TestBudgets:
    @pytest.mark.asyncio
    async def test_default_presets(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            _seed(_ctx)
            brief = await _ctx_call(session, purpose="complete briefing on Priya", detail_level="brief")
            comp = await _ctx_call(session, purpose="complete briefing on Priya", detail_level="comprehensive")
            assert brief["retrieval"]["budget_tokens"] == 750
            assert comp["retrieval"]["budget_tokens"] == 6000
            assert comp["retrieval"]["returned_items"] >= brief["retrieval"]["returned_items"]

    @pytest.mark.asyncio
    async def test_explicit_max_tokens_overrides(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            _seed(_ctx)
            r = await _ctx_call(session, purpose="complete briefing on Priya", detail_level="comprehensive", max_tokens=150)
            assert r["retrieval"]["budget_tokens"] == 150
            assert r["retrieval"]["approximate_tokens"] <= 150 or r["retrieval"]["returned_items"] == 1
            assert r["retrieval"]["truncated"] is True

    @pytest.mark.asyncio
    async def test_content_stays_within_budget(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            _seed(_ctx)
            r = await _ctx_call(session, purpose="everything about Priya", detail_level="comprehensive", max_tokens=400)
            assert r["retrieval"]["approximate_tokens"] <= 400 or r["retrieval"]["returned_items"] == 1

    @pytest.mark.asyncio
    async def test_truncation_reports_additional_items(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            _seed(_ctx)
            r = await _ctx_call(session, purpose="complete briefing on Priya", detail_level="comprehensive", max_tokens=200)
            assert r["retrieval"]["truncated"] is True
            assert r["retrieval"]["additional_matching_items"] > 0


class TestMetadataRules:
    @pytest.mark.asyncio
    async def test_default_high_confidence_omitted(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            _seed(_ctx)
            r = await _ctx_call(session, purpose="complete briefing on Priya", detail_level="comprehensive")
            for item in r["facts"] + r["relationships"]:
                assert item.get("confidence") in (None, "medium", "low")

    @pytest.mark.asyncio
    async def test_low_confidence_shown_when_present(self, tmp_path, provider):
        async with running_session(tmp_path, embedding_provider=provider) as (session, _ctx):
            _seed(_ctx)
            r = await _ctx_call(session, purpose="Is Priya learning the banjo?", detail_level="standard")
            banjo = next((f for f in r["facts"] if "banjo" in f["text"]), None)
            assert banjo is not None and banjo["confidence"] == "low"

    @pytest.mark.asyncio
    async def test_no_storage_or_index_metadata(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            _seed(_ctx)
            r = await _ctx_call(session, purpose="complete briefing on Priya", detail_level="comprehensive")
            blob = repr(r)
            for banned in ("content_hash", "embedding", "chunk_id", "file_path", "row_index", "vectors", "score"):
                assert banned not in blob

    @pytest.mark.asyncio
    async def test_person_id_appears_once(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            _seed(_ctx)
            r = await _ctx_call(session, purpose="complete briefing on Priya", detail_level="comprehensive")
            assert repr(r).count("'P0001'") == 1  # only in the person envelope

    @pytest.mark.asyncio
    async def test_text_is_verbatim(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            _seed(_ctx)
            person = _ctx.store.read_person("P0001")
            canonical = {f.id: " ".join(f.text.split()) for f in person.facts}
            r = await _ctx_call(session, purpose="complete briefing on Priya", detail_level="comprehensive")
            for f in r["facts"] + r["relationships"]:
                assert f["text"] == canonical[f["id"]]


# --------------------------------------------------------------------------
# hybrid retrieval (needs the real embedding model)
# --------------------------------------------------------------------------


class TestHybridRetrieval:
    @pytest.mark.asyncio
    async def test_semantic_available_with_provider(self, tmp_path, provider):
        async with running_session(tmp_path, embedding_provider=provider) as (session, _ctx):
            _seed(_ctx)
            r = await _ctx_call(session, purpose="who is Priya")
            assert r["retrieval"]["semantic_available"] is True

    @pytest.mark.asyncio
    async def test_search_restricted_to_the_person(self, tmp_path, provider):
        async with running_session(tmp_path, embedding_provider=provider) as (session, _ctx):
            _seed(_ctx)
            r = await _ctx_call(session, person_id="P0001", purpose="climbing friend from college")
            assert "marco" not in r["person"]["name"].lower()
            # Marco's own college-friend fact must not leak into Priya's packet
            for item in r["facts"] + r["relationships"] + r["interactions"]:
                assert "college friend of alex" not in item["text"].lower()

    @pytest.mark.asyncio
    async def test_semantic_paraphrase_retrieves_related_fact(self, tmp_path, provider):
        async with running_session(tmp_path, embedding_provider=provider) as (session, _ctx):
            _seed(_ctx)
            # no lexical overlap with "pediatric nurse / Maine Medical Center"
            r = await _ctx_call(session, purpose="where does Priya work in healthcare", detail_level="standard")
            assert any("Maine Medical Center" in f["text"] for f in r["facts"])

    @pytest.mark.asyncio
    async def test_broad_purpose_is_diverse_not_near_duplicates(self, tmp_path, provider):
        async with running_session(tmp_path, embedding_provider=provider) as (session, _ctx):
            _seed(_ctx)
            r = await _ctx_call(session, purpose="complete briefing on Priya", detail_level="comprehensive")
            sections = {f.get("section") for f in r["facts"]}
            assert len(sections) >= 3  # spans multiple categories


# --------------------------------------------------------------------------
# representative dating-contact questions (spec 13.41-48)
# --------------------------------------------------------------------------

_AS_OF = dt.date(2026, 9, 5)


class TestRepresentativeQuestions:
    @pytest.mark.asyncio
    async def test_who_and_how_we_met(self, tmp_path, provider):
        async with running_session(tmp_path, embedding_provider=provider) as (session, _ctx):
            _seed(_ctx)
            r = await _ctx_call(session, purpose="Remind me who Priya is and how I know her")
            assert r["summary"] is not None
            ids = {f["id"] for f in r["facts"] + r["relationships"]}
            assert "fact-1" in ids or "fact-2" in ids  # met on Hinge / introduced by Marco

    @pytest.mark.asyncio
    async def test_second_date_returns_interests_not_a_venue(self, tmp_path, provider):
        async with running_session(tmp_path, embedding_provider=provider) as (session, _ctx):
            _seed(_ctx)
            r = await _ctx_call(session, purpose="Suggest a good place for a second date with Priya")
            texts = " ".join(f["text"] for f in r["facts"] + r["interactions"])
            assert "climb" in texts.lower() or "sour beer" in texts.lower()
            # Montauk must not itself name a venue / recommendation
            assert "i recommend" not in repr(r).lower() and "you should" not in repr(r).lower()

    @pytest.mark.asyncio
    async def test_dog_name_returns_dog_fact_without_a_name(self, tmp_path, provider):
        async with running_session(tmp_path, embedding_provider=provider) as (session, _ctx):
            _seed(_ctx)
            r = await _ctx_call(session, purpose="What was Priya's dog's name?")
            joined = " ".join(f["text"] for f in r["facts"] + r["interactions"])
            assert "rescue dogs" in joined  # the dog fact is returned
            assert r["retrieval"]["returned_items"] <= 5  # compact, not a dump
            # Montauk returns the evidence and does not assert a dog name
            assert "dog's name is" not in repr(r).lower()
            assert "named " not in " ".join(
                f["text"] for f in r["facts"] if "dog" in f["text"].lower()
            )

    @pytest.mark.asyncio
    async def test_mountain_quote_retrieves_tower_evidence_no_fabrication(self, tmp_path, provider):
        async with running_session(tmp_path, embedding_provider=provider) as (session, _ctx):
            _seed(_ctx)
            r = await _ctx_call(session, purpose="What did Priya say when we were on top of that mountain?")
            all_text = " ".join(f["text"] for f in r["facts"] + r["interactions"])
            assert "stone tower" in all_text  # the real evidence (Fort Williams tower)
            assert '"I could stay up here all afternoon."' in all_text  # verbatim, from canonical fact

    @pytest.mark.asyncio
    async def test_reach_out_timing_returns_elapsed_time_no_verdict(self, tmp_path, provider):
        async with running_session(tmp_path, embedding_provider=provider) as (session, _ctx):
            _seed(_ctx)
            r = await _ctx_call(session, purpose="Have I waited too long to reach out to Priya again?")
            assert r["temporal"]["last_recorded_interaction"] == "2026-08-27"
            assert isinstance(r["temporal"]["days_since_last_recorded_interaction"], int)
            assert "too_long" not in repr(r["temporal"])

    @pytest.mark.asyncio
    async def test_complete_briefing_is_organized_and_honest_about_truncation(self, tmp_path, provider):
        async with running_session(tmp_path, embedding_provider=provider) as (session, _ctx):
            _seed(_ctx)
            full = await _ctx_call(session, purpose="Give me a complete briefing on Priya", detail_level="comprehensive")
            assert full["summary"] is not None
            assert full["facts"] and full["interactions"]
            tight = await _ctx_call(
                session, purpose="Give me a complete briefing on Priya", detail_level="comprehensive", max_tokens=250
            )
            assert tight["retrieval"]["truncated"] is True
            assert tight["retrieval"]["additional_matching_items"] > 0

    @pytest.mark.asyncio
    async def test_birthday_gift_returns_birthday_and_preferences_no_product(self, tmp_path, provider):
        async with running_session(tmp_path, embedding_provider=provider) as (session, _ctx):
            _seed(_ctx)
            r = await _ctx_call(session, purpose="What would be a good birthday gift for Priya?")
            texts = " ".join(f["text"] for f in r["facts"]).lower()
            assert "march" in texts  # birthday month known (day/year not)
            assert sum(k in texts for k in ("sour beer", "climb", "boulder", "banjo")) >= 2  # preferences
            assert "buy her" not in repr(r).lower() and "i suggest" not in repr(r).lower()

    @pytest.mark.asyncio
    async def test_compatibility_returns_balanced_evidence_no_judgment(self, tmp_path, provider):
        async with running_session(tmp_path, embedding_provider=provider) as (session, _ctx):
            _seed(_ctx)
            r = await _ctx_call(session, purpose="Do you think Priya and I are a good match?")
            texts = " ".join(f["text"] for f in r["facts"] + r["interactions"]).lower()
            # a reservation / awkwardness signal
            assert any(k in texts for k in ("awkward", "not sure", "not.*spark", "low-pressure", "fully over"))
            # a positive signal
            assert any(k in texts for k in ("great time", "four texts in a row", "wanting", "burst of texts", "do it again"))
            # Montauk itself renders no verdict
            assert "you are a good match" not in repr(r).lower()
            assert "compatib" not in " ".join(f["text"] for f in r["facts"]).lower()
