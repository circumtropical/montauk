import pytest

from _helpers import call, running_session
from montauk.embeddings.local import LocalEmbeddingProvider


@pytest.fixture(scope="module")
def provider() -> LocalEmbeddingProvider:
    return LocalEmbeddingProvider()


class TestHybridSearchPeople:
    @pytest.mark.asyncio
    async def test_vague_description_surfaces_two_same_named_candidates_with_distinct_evidence(
        self, tmp_path, provider
    ):
        # The scenario from spec section 19/22: two people who share a
        # display name, distinguishable only by remembered context, not
        # by exact name/alias matching.
        async with running_session(tmp_path, embedding_provider=provider) as (session, _ctx):
            await call(
                session,
                "create_person",
                name="Mike Chen",
                summary="MIT classmate who now works in robotics.",
            )
            await call(
                session,
                "create_person",
                name="Mike Chen",
                summary="Stanford alum; startup founder in fintech.",
            )

            result = await call(session, "search_people", query="the robotics guy I met through MIT")

            person_ids = {c["person_id"] for c in result["candidates"]}
            assert "mike-chen" in person_ids
            assert "mike-chen-2" in person_ids

            by_id = {c["person_id"]: c for c in result["candidates"]}
            assert by_id["mike-chen"]["match_evidence"] != by_id["mike-chen-2"]["match_evidence"]
            # The robotics one should rank first given the query.
            assert result["candidates"][0]["person_id"] == "mike-chen"

    @pytest.mark.asyncio
    async def test_semantic_match_surfaces_person_with_no_exact_substring_hit(self, tmp_path, provider):
        async with running_session(tmp_path, embedding_provider=provider) as (session, _ctx):
            await call(
                session,
                "create_person",
                name="Sarah Jones",
                summary="Runs a small bakery downtown, known for sourdough.",
            )

            # Query shares no substring with "Sarah Jones" or the summary text.
            result = await call(session, "search_people", query="pastry chef who bakes bread")

            assert any(c["person_id"] == "sarah-jones" for c in result["candidates"])
            evidence = next(c for c in result["candidates"] if c["person_id"] == "sarah-jones")["match_evidence"]
            assert any("semantic match" in e for e in evidence)

    @pytest.mark.asyncio
    async def test_exact_and_semantic_evidence_merge_for_the_same_person(self, tmp_path, provider):
        async with running_session(tmp_path, embedding_provider=provider) as (session, _ctx):
            await call(
                session,
                "create_person",
                name="Homer Simpson",
                summary="Neighbor who works at the power plant.",
            )
            await call(session, "add_fact", person_id="homer-simpson", category="Interests", text="Loves donuts.")

            result = await call(session, "search_people", query="Homer")

            homer = next(c for c in result["candidates"] if c["person_id"] == "homer-simpson")
            assert any("name matches" in e for e in homer["match_evidence"])

    @pytest.mark.asyncio
    async def test_results_capped_at_max_candidates(self, tmp_path, provider):
        async with running_session(tmp_path, embedding_provider=provider) as (session, ctx):
            ctx.max_candidates = 2
            for i in range(5):
                await call(session, "create_person", name=f"Gil Gunderson {i}", summary="Sells cars.")

            result = await call(session, "search_people", query="car salesman")
            assert len(result["candidates"]) <= 2

    @pytest.mark.asyncio
    async def test_no_results_when_nothing_matches_semantically_or_exactly(self, tmp_path, provider):
        async with running_session(tmp_path, embedding_provider=provider) as (session, ctx):
            ctx.similarity_threshold = 0.9  # strict, so an unrelated query won't pass
            await call(session, "create_person", name="Homer Simpson", summary="Works at the power plant.")

            result = await call(session, "search_people", query="astrophysics conference in Geneva")
            assert result["candidates"] == []

    @pytest.mark.asyncio
    async def test_archived_person_does_not_appear_in_semantic_results(self, tmp_path, provider):
        async with running_session(tmp_path, embedding_provider=provider) as (session, _ctx):
            await call(session, "create_person", name="Frank Grimes", summary="Engineer at the power plant.")
            await call(session, "archive_person", person_id="frank-grimes")

            result = await call(session, "search_people", query="engineer power plant")
            assert result["candidates"] == []
