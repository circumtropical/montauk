import asyncio
import re

import pytest

from _helpers import call, call_expecting_error, running_session


def _local_id_num(local_id: str) -> int:
    return int(re.search(r"-(\d+)$", local_id).group(1))


CORE_TOOL_NAMES = {
    "search_people",
    "get_person",
    "get_full_record",
    "get_facts",
    "get_interactions",
    "create_person",
    "add_fact",
    "update_fact",
    "remove_fact",
    "record_interaction",
    "update_contact_details",
    "update_summary",
    "set_birthday",
    "set_contact_cadence",
    "update_person_batch",
}

OPS_TOOL_NAMES = {
    "get_upcoming_birthdays",
    "list_overdue_contacts",
    "archive_person",
    "list_archived_people",
    "get_archived_person",
    "validate_repository",
    "get_validation_errors",
    "get_health_status",
}

ALL_TOOL_NAMES = CORE_TOOL_NAMES | OPS_TOOL_NAMES


class TestToolRegistration:
    @pytest.mark.asyncio
    async def test_full_tool_surface_registered(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            tools = (await session.list_tools()).tools
            names = {t.name for t in tools}
            assert names == ALL_TOOL_NAMES

    @pytest.mark.asyncio
    async def test_every_tool_has_a_nonempty_description(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            tools = (await session.list_tools()).tools
            for tool in tools:
                assert tool.description and len(tool.description) > 20, tool.name


class TestCreatePerson:
    @pytest.mark.asyncio
    async def test_create_and_get(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            result = await call(session, "create_person", name="Lisa Simpson", summary="Plays sax.")
            assert result["person_id"] == "lisa-simpson"
            assert result["index_update_status"] == "ok"

            person = await call(session, "get_person", person_id="lisa-simpson")
            assert person["name"] == "Lisa Simpson"
            assert person["summary"] == "Plays sax."
            assert "facts" not in person  # PersonCore excludes facts/interactions

    @pytest.mark.asyncio
    async def test_duplicate_name_gets_suffixed_id(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            r1 = await call(session, "create_person", name="Gil Gunderson")
            r2 = await call(session, "create_person", name="Gil Gunderson")
            assert r1["person_id"] == "gil-gunderson"
            assert r2["person_id"] == "gil-gunderson-2"

    @pytest.mark.asyncio
    async def test_invalid_cadence_is_a_validation_error(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            text = await call_expecting_error(
                session, "create_person", name="Bad Cadence", desired_contact_cadence_days=-5
            )
            assert "VALIDATION_ERROR" in text


class TestGetPersonErrors:
    @pytest.mark.asyncio
    async def test_not_found(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            text = await call_expecting_error(session, "get_person", person_id="nobody")
            assert "NOT_FOUND" in text


class TestFacts:
    @pytest.mark.asyncio
    async def test_add_get_update_remove_fact(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")

            add_result = await call(
                session, "add_fact", person_id="homer-simpson", category="Family", text="Married to Marge."
            )
            assert add_result["changed_ids"] == ["fact-1"]

            facts = await call(session, "get_facts", person_id="homer-simpson")
            assert len(facts) == 1
            assert facts[0]["text"] == "Married to Marge."
            assert facts[0]["confidence"] == "high"

            await call(
                session,
                "update_fact",
                person_id="homer-simpson",
                fact_id="fact-1",
                text="Married to Marge Simpson.",
                confidence="medium",
            )
            facts = await call(session, "get_facts", person_id="homer-simpson")
            assert facts[0]["text"] == "Married to Marge Simpson."
            assert facts[0]["confidence"] == "medium"

            await call(session, "remove_fact", person_id="homer-simpson", fact_id="fact-1")
            facts = await call(session, "get_facts", person_id="homer-simpson")
            assert facts == []

    @pytest.mark.asyncio
    async def test_get_facts_filtered_by_category(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            await call(session, "add_fact", person_id="homer-simpson", category="Family", text="A")
            await call(session, "add_fact", person_id="homer-simpson", category="Interests", text="B")

            family_only = await call(session, "get_facts", person_id="homer-simpson", category="Family")
            assert [f["text"] for f in family_only] == ["A"]

    @pytest.mark.asyncio
    async def test_invalid_category_is_validation_error(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            text = await call_expecting_error(
                session, "add_fact", person_id="homer-simpson", category="Not A Category", text="x"
            )
            assert "VALIDATION_ERROR" in text

    @pytest.mark.asyncio
    async def test_remove_nonexistent_fact_is_not_found(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            text = await call_expecting_error(session, "remove_fact", person_id="homer-simpson", fact_id="fact-99")
            assert "NOT_FOUND" in text

    @pytest.mark.asyncio
    async def test_invalid_update_leaves_file_untouched(self, tmp_path):
        async with running_session(tmp_path) as (session, ctx):
            await call(session, "create_person", name="Homer Simpson")
            await call(session, "add_fact", person_id="homer-simpson", category="Family", text="Original text.")
            before = ctx.store.person_path("homer-simpson").read_text()

            await call_expecting_error(
                session, "update_fact", person_id="homer-simpson", fact_id="fact-1", category="Nonexistent Category"
            )

            after = ctx.store.person_path("homer-simpson").read_text()
            assert before == after


class TestInteractions:
    @pytest.mark.asyncio
    async def test_record_and_get_interactions_most_recent_first(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            await call(session, "record_interaction", person_id="homer-simpson", date="2024", summary="old")
            await call(session, "record_interaction", person_id="homer-simpson", date="2026-08-20", summary="new")

            interactions = await call(session, "get_interactions", person_id="homer-simpson")
            assert [i["summary"] for i in interactions] == ["new", "old"]

    @pytest.mark.asyncio
    async def test_get_interactions_respects_limit(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            for year in ("2020", "2021", "2022"):
                await call(session, "record_interaction", person_id="homer-simpson", date=year)

            limited = await call(session, "get_interactions", person_id="homer-simpson", limit=1)
            assert len(limited) == 1
            assert limited[0]["date"] == "2022"

    @pytest.mark.asyncio
    async def test_record_interaction_worth_it_even_with_no_new_facts(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            result = await call(session, "record_interaction", person_id="homer-simpson", date="2026-08-20")
            assert result["changed_ids"] == ["int-1"]


class TestStructuredFieldUpdates:
    @pytest.mark.asyncio
    async def test_update_contact_details_partial(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            await call(session, "update_contact_details", person_id="homer-simpson", emails=["homer@example.com"])
            await call(session, "update_contact_details", person_id="homer-simpson", phones=["+1-555-0100"])

            person = await call(session, "get_person", person_id="homer-simpson")
            assert person["contact"]["emails"] == ["homer@example.com"]
            assert person["contact"]["phones"] == ["+1-555-0100"]

    @pytest.mark.asyncio
    async def test_update_summary(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            await call(session, "update_summary", person_id="homer-simpson", summary="Updated.")
            person = await call(session, "get_person", person_id="homer-simpson")
            assert person["summary"] == "Updated."

    @pytest.mark.asyncio
    async def test_set_and_clear_birthday(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            await call(session, "set_birthday", person_id="homer-simpson", birthday="1956-05-12")
            person = await call(session, "get_person", person_id="homer-simpson")
            assert person["birthday"] == "1956-05-12"

            await call(session, "set_birthday", person_id="homer-simpson", birthday=None)
            person = await call(session, "get_person", person_id="homer-simpson")
            assert person["birthday"] is None

    @pytest.mark.asyncio
    async def test_set_contact_cadence(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            await call(session, "set_contact_cadence", person_id="homer-simpson", desired_contact_cadence_days=30)
            person = await call(session, "get_person", person_id="homer-simpson")
            assert person["desired_contact_cadence_days"] == 30


class TestGetFullRecord:
    @pytest.mark.asyncio
    async def test_returns_canonical_markdown_text(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            await call(session, "add_fact", person_id="homer-simpson", category="Family", text="Married to Marge.")

            record = await call(session, "get_full_record", person_id="homer-simpson")
            assert isinstance(record, str)
            assert "id: homer-simpson" in record
            assert "## Family" in record
            assert "Married to Marge." in record


class TestSearchPeople:
    @pytest.mark.asyncio
    async def test_matches_by_name_substring(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            await call(session, "create_person", name="Marge Simpson")

            result = await call(session, "search_people", query="homer")
            assert [c["person_id"] for c in result["candidates"]] == ["homer-simpson"]
            assert "matches" in result["candidates"][0]["match_evidence"][0]

    @pytest.mark.asyncio
    async def test_multiple_candidates_returned_for_shared_name(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Gil Gunderson")
            await call(session, "create_person", name="Gil Gunderson")

            result = await call(session, "search_people", query="Gil")
            assert {c["person_id"] for c in result["candidates"]} == {"gil-gunderson", "gil-gunderson-2"}

    @pytest.mark.asyncio
    async def test_no_match_returns_empty_candidates(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            result = await call(session, "search_people", query="nonexistent")
            assert result["candidates"] == []


class TestUpdatePersonBatch:
    @pytest.mark.asyncio
    async def test_batch_applies_multiple_operation_kinds_atomically(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")

            result = await call(
                session,
                "update_person_batch",
                person_id="homer-simpson",
                operations=[
                    {"op": "add_fact", "category": "Family", "text": "Married to Marge."},
                    {"op": "record_interaction", "date": "2026-08-20", "summary": "Lunch at Moe's."},
                    {"op": "update_summary", "summary": "Neighbor and old friend."},
                    {"op": "set_birthday", "birthday": "1956-05-12"},
                    {"op": "set_contact_cadence", "desired_contact_cadence_days": 14},
                    {"op": "update_contact_details", "emails": ["homer@example.com"]},
                ],
            )
            assert set(result["changed_ids"]) == {"fact-1", "int-1"}

            person = await call(session, "get_person", person_id="homer-simpson")
            assert person["summary"] == "Neighbor and old friend."
            assert person["birthday"] == "1956-05-12"
            assert person["desired_contact_cadence_days"] == 14
            assert person["contact"]["emails"] == ["homer@example.com"]

            facts = await call(session, "get_facts", person_id="homer-simpson")
            assert [f["text"] for f in facts] == ["Married to Marge."]
            interactions = await call(session, "get_interactions", person_id="homer-simpson")
            assert [i["summary"] for i in interactions] == ["Lunch at Moe's."]

    @pytest.mark.asyncio
    async def test_batch_can_add_update_and_remove_facts_together(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            await call(session, "add_fact", person_id="homer-simpson", category="Family", text="Keep me.")
            await call(session, "add_fact", person_id="homer-simpson", category="Interests", text="Remove me.")

            result = await call(
                session,
                "update_person_batch",
                person_id="homer-simpson",
                operations=[
                    {"op": "update_fact", "fact_id": "fact-1", "text": "Keep me, updated."},
                    {"op": "remove_fact", "fact_id": "fact-2"},
                    {"op": "add_fact", "category": "Life Events", "text": "Brand new fact."},
                ],
            )
            assert set(result["changed_ids"]) == {"fact-1", "fact-2", "fact-3"}

            facts = await call(session, "get_facts", person_id="homer-simpson")
            texts = {f["id"]: f["text"] for f in facts}
            assert texts == {"fact-1": "Keep me, updated.", "fact-3": "Brand new fact."}

    @pytest.mark.asyncio
    async def test_one_invalid_operation_rolls_back_the_whole_batch(self, tmp_path):
        async with running_session(tmp_path) as (session, ctx):
            await call(session, "create_person", name="Homer Simpson")
            before = ctx.store.person_path("homer-simpson").read_text()

            text = await call_expecting_error(
                session,
                "update_person_batch",
                person_id="homer-simpson",
                operations=[
                    {"op": "add_fact", "category": "Family", "text": "This would have been valid."},
                    {"op": "update_fact", "fact_id": "fact-99", "text": "This one does not exist."},
                ],
            )
            assert "NOT_FOUND" in text

            after = ctx.store.person_path("homer-simpson").read_text()
            assert before == after  # zero changes applied, not even the valid one
            facts = await call(session, "get_facts", person_id="homer-simpson")
            assert facts == []

    @pytest.mark.asyncio
    async def test_invalid_discriminator_is_rejected_by_schema(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            result = await session.call_tool(
                "update_person_batch",
                {"person_id": "homer-simpson", "operations": [{"op": "delete_everything"}]},
            )
            assert result.is_error

    @pytest.mark.asyncio
    async def test_batch_is_scoped_to_one_person_only(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            await call(session, "create_person", name="Marge Simpson")

            await call(
                session,
                "update_person_batch",
                person_id="homer-simpson",
                operations=[{"op": "update_summary", "summary": "Only Homer changes."}],
            )

            homer = await call(session, "get_person", person_id="homer-simpson")
            marge = await call(session, "get_person", person_id="marge-simpson")
            assert homer["summary"] == "Only Homer changes."
            assert marge["summary"] is None


class TestConcurrentWrites:
    @pytest.mark.asyncio
    async def test_concurrent_add_fact_calls_do_not_corrupt_the_file(self, tmp_path):
        async with running_session(tmp_path) as (session, ctx):
            await call(session, "create_person", name="Homer Simpson")

            async def add(i: int):
                return await call(
                    session, "add_fact", person_id="homer-simpson", category="General Notes", text=f"note {i}"
                )

            results = await asyncio.gather(*(add(i) for i in range(10)))
            changed_ids = sorted({r["changed_ids"][0] for r in results}, key=_local_id_num)
            assert changed_ids == [f"fact-{i}" for i in range(1, 11)]

            # The file must parse cleanly and contain exactly the 10 facts,
            # with no duplicated or dropped IDs from a lost update.
            person = ctx.store.read_person("homer-simpson")
            assert sorted((f.id for f in person.facts), key=_local_id_num) == [f"fact-{i}" for i in range(1, 11)]
