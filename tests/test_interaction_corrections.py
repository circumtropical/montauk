"""update_interaction / remove_interaction (amendment): correcting
inaccurate data is not erasing history."""

import pytest
from _helpers import call, call_expecting_error, running_session


async def _person(session, name: str) -> str:
    return (await call(session, "create_person", name=name))["person_id"]


class TestUpdateInteraction:
    @pytest.mark.asyncio
    async def test_corrects_summary_and_date_without_changing_interaction_id(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            pid = await _person(session, "Alice")
            rec = await call(session, "record_interaction", person_id=pid, date="2026-01-01", summary="Lunhc")
            iid = rec["changed_ids"][0]

            result = await call(
                session,
                "update_interaction",
                person_id=pid,
                interaction_id=iid,
                summary="Lunch downtown",
                date="2026-01-02",
                correction_reason="typo + wrong day",
            )
            assert result["operation"] == "updated"
            assert result["interaction_id"] == iid

            interactions = await call(session, "get_interactions", person_id=pid)
            assert interactions[0]["id"] == iid
            assert interactions[0]["summary"] == "Lunch downtown"
            assert interactions[0]["date"] == "2026-01-02"

    @pytest.mark.asyncio
    async def test_omitted_fields_are_unchanged(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            pid = await _person(session, "Alice")
            rec = await call(
                session,
                "record_interaction",
                person_id=pid,
                date="2026-01-01",
                channel="phone",
                summary="Caught up",
            )
            iid = rec["changed_ids"][0]

            await call(session, "update_interaction", person_id=pid, interaction_id=iid, summary="Caught up properly")

            i = (await call(session, "get_interactions", person_id=pid))[0]
            assert i["channel"] == "phone"  # untouched
            assert i["date"] == "2026-01-01"

    @pytest.mark.asyncio
    async def test_invalid_result_is_rejected_with_no_partial_write(self, tmp_path):
        async with running_session(tmp_path) as (session, ctx):
            pid = await _person(session, "Alice")
            rec = await call(session, "record_interaction", person_id=pid, date="2026-01-01", summary="ok")
            iid = rec["changed_ids"][0]
            before = ctx.store.person_path(pid).read_text()

            text = await call_expecting_error(
                session, "update_interaction", person_id=pid, interaction_id=iid, connection_level=99
            )
            assert "VALIDATION_ERROR" in text
            assert ctx.store.person_path(pid).read_text() == before

    @pytest.mark.asyncio
    async def test_missing_interaction_is_not_found(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            pid = await _person(session, "Alice")
            text = await call_expecting_error(
                session, "update_interaction", person_id=pid, interaction_id="int-99", summary="x"
            )
            assert "NOT_FOUND" in text


class TestParticipantCorrection:
    @pytest.mark.asyncio
    async def test_monica_to_monique_leaves_no_trace_on_monica(self, tmp_path):
        async with running_session(tmp_path, embedding_provider=None) as (session, ctx):
            monica = await _person(session, "Monica")
            monique = await _person(session, "Monique")
            rec = await call(
                session,
                "record_interaction",
                person_id=monica,
                date="2026-03-03",
                summary="Coffee and a long catch-up",
            )
            iid = rec["changed_ids"][0]

            result = await call(
                session,
                "update_interaction",
                person_id=monica,
                interaction_id=iid,
                move_to_person_id=monique,
                correction_reason="it was Monique, not Monica",
            )
            assert result["operation"] == "reattributed"
            assert result["moved_to_person_id"] == monique
            new_iid = result["new_interaction_id"]

            monica_ints = await call(session, "get_interactions", person_id=monica)
            monique_ints = await call(session, "get_interactions", person_id=monique)
            assert monica_ints == []
            assert [i["id"] for i in monique_ints] == [new_iid]
            assert monique_ints[0]["summary"] == "Coffee and a long catch-up"

            # No voided/tombstone trace of the misattribution on Monica.
            monica_record = await call(session, "get_full_record", person_id=monica)
            assert "catch-up" not in monica_record
            # And the sqlite index reflects the move.
            assert ctx.sqlite_index.get_row(monica)["last_interaction_at"] is None
            assert ctx.sqlite_index.get_row(monique)["last_interaction_at"] == "2026-03-03"

    @pytest.mark.asyncio
    async def test_reattribution_target_must_exist(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            pid = await _person(session, "Alice")
            rec = await call(session, "record_interaction", person_id=pid, date="2026-01-01", summary="x")
            iid = rec["changed_ids"][0]
            text = await call_expecting_error(
                session,
                "update_interaction",
                person_id=pid,
                interaction_id=iid,
                move_to_person_id="P0404",
            )
            assert "NOT_FOUND" in text


class TestRemoveInteraction:
    @pytest.mark.asyncio
    async def test_removes_wholly_erroneous_interaction_everywhere(self, tmp_path):
        async with running_session(tmp_path) as (session, ctx):
            pid = await _person(session, "Alice")
            rec = await call(
                session, "record_interaction", person_id=pid, date="2026-01-01", summary="Never happened"
            )
            iid = rec["changed_ids"][0]

            result = await call(
                session,
                "remove_interaction",
                person_id=pid,
                interaction_id=iid,
                correction_reason="this meeting never took place",
            )
            assert result["operation"] == "removed"

            assert await call(session, "get_interactions", person_id=pid) == []
            record = await call(session, "get_full_record", person_id=pid)
            assert "Never happened" not in record
            assert ctx.sqlite_index.get_row(pid)["last_interaction_at"] is None

    @pytest.mark.asyncio
    async def test_removing_a_duplicate_leaves_the_original(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            pid = await _person(session, "Alice")
            a = (await call(session, "record_interaction", person_id=pid, date="2026-01-01", summary="Real one"))[
                "changed_ids"
            ][0]
            b = (await call(session, "record_interaction", person_id=pid, date="2026-01-01", summary="Real one"))[
                "changed_ids"
            ][0]
            await call(
                session, "remove_interaction", person_id=pid, interaction_id=b, correction_reason="duplicate of " + a
            )
            remaining = await call(session, "get_interactions", person_id=pid)
            assert [i["id"] for i in remaining] == [a]

    @pytest.mark.asyncio
    async def test_requires_a_correction_reason(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            pid = await _person(session, "Alice")
            rec = await call(session, "record_interaction", person_id=pid, date="2026-01-01", summary="x")
            iid = rec["changed_ids"][0]
            text = await call_expecting_error(
                session, "remove_interaction", person_id=pid, interaction_id=iid, correction_reason="   "
            )
            assert "VALIDATION_ERROR" in text


class TestGuidance:
    @pytest.mark.asyncio
    async def test_tool_descriptions_distinguish_update_from_remove(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            tools = {t.name: t for t in (await session.list_tools()).tools}
            upd = tools["update_interaction"].description
            rem = tools["remove_interaction"].description
            assert "wrong participant" in upd and "move_to_person_id" in upd
            assert "erroneous" in rem and "duplicates another" in rem
            assert "old, inconvenient, sensitive" in rem

    def test_server_instructions_protect_accurate_history(self):
        from montauk.server import SERVER_INSTRUCTIONS

        assert "Correcting inaccurate data is not erasing history" in SERVER_INSTRUCTIONS
        assert (
            "Never remove an accurate interaction merely because it is old, inconvenient, sensitive"
            in SERVER_INSTRUCTIONS
        )

    @pytest.mark.asyncio
    async def test_removed_interaction_not_returned_by_ordinary_retrieval(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            pid = await _person(session, "Alice")
            iid = (await call(session, "record_interaction", person_id=pid, date="2026-01-01", summary="Gone soon"))[
                "changed_ids"
            ][0]
            await call(
                session, "remove_interaction", person_id=pid, interaction_id=iid, correction_reason="accident"
            )
            assert await call(session, "get_interactions", person_id=pid, limit=10) == []
