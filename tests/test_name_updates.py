"""Mutable display names + aliases (amendment): identity (person_id) is
permanent; the name is the best info currently known and can be corrected
without recreating the record."""

import pytest

from _helpers import call, call_expecting_error, running_session


async def _homer(session) -> str:
    return (await call(session, "create_person", name="Homer Simpson"))["person_id"]


class TestUpdatePersonName:
    @pytest.mark.asyncio
    async def test_corrects_a_misspelled_name_without_changing_identity_or_file(self, tmp_path):
        async with running_session(tmp_path) as (session, ctx):
            pid = (await call(session, "create_person", name="Homer Simsonp"))["person_id"]
            path_before = ctx.store.person_path(pid)

            result = await call(session, "update_person_name", person_id=pid, name="Homer Simpson")

            assert result["person_id"] == pid
            assert result["name"] == "Homer Simpson"
            assert "Homer Simsonp" in result["aliases"]  # previous name retained
            assert ctx.store.person_path(pid) == path_before
            assert ctx.store.person_path(pid).exists()

            person = await call(session, "get_person", person_id=pid)
            assert person["name"] == "Homer Simpson"

    @pytest.mark.asyncio
    async def test_can_be_created_with_only_a_first_name_then_completed(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            pid = (await call(session, "create_person", name="Catherine"))["person_id"]
            await call(session, "update_person_name", person_id=pid, name="Catherine Nguyen")
            person = await call(session, "get_person", person_id=pid)
            assert person["name"] == "Catherine Nguyen"
            assert person["aliases"] == ["Catherine"]

    @pytest.mark.asyncio
    async def test_caller_can_decline_retaining_the_old_name(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            pid = (await call(session, "create_person", name="Mr Wrong Person"))["person_id"]
            result = await call(
                session,
                "update_person_name",
                person_id=pid,
                name="Real Person",
                retain_previous_as_alias=False,
            )
            assert result["aliases"] == []

    @pytest.mark.asyncio
    async def test_aliases_added_and_removed_atomically_with_rename(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            pid = (await call(session, "create_person", name="Bob", aliases=["Bobby", "Robert"]))[
                "person_id"
            ]
            result = await call(
                session,
                "update_person_name",
                person_id=pid,
                name="Robert Paulson",
                aliases_to_add=["Big Bob"],
                aliases_to_remove=["Robert"],
            )
            assert set(result["aliases"]) == {"Bob", "Bobby", "Big Bob"}

    @pytest.mark.asyncio
    async def test_identical_names_allowed_and_reported_as_possible_duplicates(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="John Smith")
            pid2 = (await call(session, "create_person", name="Jon Smith"))["person_id"]
            result = await call(session, "update_person_name", person_id=pid2, name="John Smith")
            assert result["name"] == "John Smith"  # not rejected
            assert [d["person_id"] for d in result["possible_duplicates"]] == ["P0001"]

    @pytest.mark.asyncio
    async def test_rename_preserves_facts_and_interactions(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            pid = await _homer(session)
            await call(session, "add_fact", person_id=pid, category="Family", text="Has three kids.")
            await call(session, "record_interaction", person_id=pid, date="2026-01-01", summary="Lunch.")

            await call(session, "update_person_name", person_id=pid, name="Homer J. Simpson")

            facts = await call(session, "get_facts", person_id=pid)
            interactions = await call(session, "get_interactions", person_id=pid)
            assert [f["text"] for f in facts] == ["Has three kids."]
            assert [i["summary"] for i in interactions] == ["Lunch."]

    @pytest.mark.asyncio
    async def test_new_name_and_alias_are_searchable_after_rename(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            pid = (await call(session, "create_person", name="Cathy"))["person_id"]
            await call(session, "update_person_name", person_id=pid, name="Catherine Nguyen")

            by_new = await call(session, "search_people", query="Catherine Nguyen")
            by_alias = await call(session, "search_people", query="Cathy")
            assert any(c["person_id"] == pid for c in by_new["candidates"])
            assert any(c["person_id"] == pid for c in by_alias["candidates"])

    @pytest.mark.asyncio
    async def test_rejects_name_derived_person_id(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            text = await call_expecting_error(
                session, "update_person_name", person_id="homer-simpson", name="Homer J. Simpson"
            )
            assert "NOT_FOUND" in text or "VALIDATION_ERROR" in text

    @pytest.mark.asyncio
    async def test_description_says_identity_is_unchanged(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            tools = {t.name: t for t in (await session.list_tools()).tools}
            desc = tools["update_person_name"].description
            assert "person_id" in desc and "unchanged" in desc
            assert "not archive-and-recreate" in desc

    @pytest.mark.asyncio
    async def test_archive_description_is_not_a_rename_workflow(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            tools = {t.name: t for t in (await session.list_tools()).tools}
            # server instructions steer agents away from archive-to-rename
            from montauk.server import SERVER_INSTRUCTIONS

            assert "Never archive and recreate a person merely to correct or complete their name" in (
                SERVER_INSTRUCTIONS
            )


class TestSetNameBatchOp:
    @pytest.mark.asyncio
    async def test_batch_set_name_uses_the_same_rename_service(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            pid = await _homer(session)
            await call(
                session,
                "update_person_batch",
                person_id=pid,
                operations=[
                    {"op": "set_name", "name": "Homer J. Simpson"},
                    {"op": "add_fact", "category": "General Notes", "text": "Goes by his middle initial now."},
                ],
            )
            person = await call(session, "get_person", person_id=pid)
            assert person["name"] == "Homer J. Simpson"
            assert person["aliases"] == ["Homer Simpson"]

    @pytest.mark.asyncio
    async def test_batch_set_name_cannot_change_person_id(self, tmp_path):
        async with running_session(tmp_path) as (session, ctx):
            pid = await _homer(session)
            await call(
                session,
                "update_person_batch",
                person_id=pid,
                operations=[{"op": "set_name", "name": "Someone Else Entirely"}],
            )
            assert ctx.store.exists(pid)
            assert ctx.store.list_person_ids() == [pid]
