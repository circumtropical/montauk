"""Generic person-ID allocation (amendment): permanent, sequential,
never name-derived, never reused."""

import asyncio
import json

import pytest
from _helpers import call, running_session

from montauk.ids import PERSON_ID_RE
from montauk.markdown_store import MarkdownStore, PersonIdSequence


class TestPersonIdSequence:
    def test_first_allocation_is_p0001(self, tmp_path):
        seq = PersonIdSequence(tmp_path)
        assert seq.allocate() == "P0001"
        assert seq.allocate() == "P0002"

    def test_allocation_persists_across_instances(self, tmp_path):
        PersonIdSequence(tmp_path).allocate()
        PersonIdSequence(tmp_path).allocate()
        assert PersonIdSequence(tmp_path).allocate() == "P0003"

    def test_gap_from_a_failed_creation_is_never_reused(self, tmp_path):
        seq = PersonIdSequence(tmp_path)
        assert seq.allocate() == "P0001"
        seq.allocate()  # P0002 "issued" but imagine the person write then failed
        # A fresh process must not hand P0002 back out.
        assert PersonIdSequence(tmp_path).allocate() == "P0003"

    def test_continues_past_p9999(self, tmp_path):
        seq = PersonIdSequence(tmp_path)
        seq.ensure_at_least(9999)
        assert seq.allocate() == "P10000"

    def test_ensure_at_least_only_raises_never_lowers(self, tmp_path):
        seq = PersonIdSequence(tmp_path)
        seq.ensure_at_least(50)
        assert seq.high_water() == 50
        assert seq.ensure_at_least(10) is False
        assert seq.high_water() == 50

    def test_corrupt_file_reads_as_zero(self, tmp_path):
        seq = PersonIdSequence(tmp_path)
        seq.path.write_text("not json", encoding="utf-8")
        assert seq.high_water() == 0


class TestStoreSyncIdSequence:
    def test_sync_raises_sequence_to_cover_hand_added_files(self, tmp_path):
        store = MarkdownStore(tmp_path / "data")
        (store.people_dir / "P0007.md").write_text(
            "---\nid: P0007\nname: Added By Hand\n---\n\n# Added By Hand\n\n## Interactions\n",
            encoding="utf-8",
        )
        assert store.sync_id_sequence() is True
        assert store.id_sequence.allocate() == "P0008"

    def test_sync_considers_archived_ids_too(self, tmp_path):
        store = MarkdownStore(tmp_path / "data")
        (store.archive_dir / "P0042.md").write_text(
            "---\nid: P0042\nname: Archived\n---\n\n# Archived\n\n## Interactions\n", encoding="utf-8"
        )
        store.sync_id_sequence()
        assert store.id_sequence.allocate() == "P0043"


class TestCreatePersonAllocation:
    @pytest.mark.asyncio
    async def test_sequential_and_regex_valid(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            ids = [
                (await call(session, "create_person", name=f"Person {i}"))["person_id"]
                for i in range(3)
            ]
        assert ids == ["P0001", "P0002", "P0003"]
        assert all(PERSON_ID_RE.match(pid) for pid in ids)

    @pytest.mark.asyncio
    async def test_concurrent_creates_get_distinct_ids(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            results = await asyncio.gather(
                *(call(session, "create_person", name=f"P{i}") for i in range(12))
            )
        ids = {r["person_id"] for r in results}
        assert len(ids) == 12
        assert ids == {f"P{n:04d}" for n in range(1, 13)}

    @pytest.mark.asyncio
    async def test_id_not_reused_after_archive_within_a_session(self, tmp_path):
        async with running_session(tmp_path) as (session, ctx):
            await call(session, "create_person", name="Gone")
            await call(session, "archive_person", person_id="P0001")
            r = await call(session, "create_person", name="New")
            assert r["person_id"] == "P0002"
            seq = json.loads(ctx.store.id_sequence.path.read_text())
            assert seq["last_allocated"] == 2
