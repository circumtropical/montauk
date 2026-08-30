import time
from pathlib import Path

from montauk.markdown_store import MarkdownStore
from montauk.models import Fact, Interaction, Person
from montauk.reconciliation import scan_people_directory
from montauk.sqlite_index import SqliteIndex


def _store(tmp_path: Path) -> MarkdownStore:
    return MarkdownStore(tmp_path / "data")


def _homer() -> Person:
    return Person(
        id="homer-simpson",
        name="Homer Simpson",
        aliases=["Homie"],
        birthday="1956-05-12",
        location="Springfield",
        company="Springfield Nuclear Power Plant",
        job_title="Safety Inspector",
        desired_contact_cadence_days=7,
        summary="Neighbor and old friend.",
        interactions=[
            Interaction(id="int-1", date="2024"),
            Interaction(id="int-2", date="2026-01-05"),
            Interaction(id="int-3", date="2025-06"),
        ],
    )


def _no_year_birthday_person() -> Person:
    return Person(id="ned-flanders", name="Ned Flanders", birthday="05-11")


def _drop_updated_at(row: dict) -> dict:
    return {k: v for k, v in row.items() if k != "updated_at"}


class TestRebuildFromScan:
    def test_rebuild_populates_expected_fields(self, tmp_path):
        store = _store(tmp_path)
        store.write_person(_homer())
        index = SqliteIndex(tmp_path / "data" / "index" / "relationships.sqlite")

        index.rebuild_from_scan(store, scan_people_directory(store))

        row = index.get_row("homer-simpson")
        assert row is not None
        assert row["name"] == "Homer Simpson"
        assert row["aliases_json"] == '["Homie"]'
        assert (row["birthday_month"], row["birthday_day"], row["birthday_year"]) == (5, 12, 1956)
        assert row["desired_contact_cadence_days"] == 7
        # last_interaction_at picks the most recent by latest-precision anchor (2026-01-05).
        assert row["last_interaction_at"] == "2026-01-05"
        assert "days_since_last_interaction" not in row  # never persisted; computed at query time

    def test_birthday_without_year_stores_null_year(self, tmp_path):
        store = _store(tmp_path)
        store.write_person(_no_year_birthday_person())
        index = SqliteIndex(tmp_path / "data" / "index" / "relationships.sqlite")

        index.rebuild_from_scan(store, scan_people_directory(store))

        row = index.get_row("ned-flanders")
        assert (row["birthday_month"], row["birthday_day"], row["birthday_year"]) == (5, 11, None)

    def test_person_with_no_interactions_has_null_last_interaction(self, tmp_path):
        store = _store(tmp_path)
        store.write_person(Person(id="moe-szyslak", name="Moe Szyslak"))
        index = SqliteIndex(tmp_path / "data" / "index" / "relationships.sqlite")

        index.rebuild_from_scan(store, scan_people_directory(store))

        assert index.get_row("moe-szyslak")["last_interaction_at"] is None

    def test_malformed_file_excluded_from_index(self, tmp_path):
        store = _store(tmp_path)
        store.write_person(_homer())
        (store.people_dir / "broken.md").write_text('---\nid: [unterminated\n---\n\n# X\n')
        index = SqliteIndex(tmp_path / "data" / "index" / "relationships.sqlite")

        index.rebuild_from_scan(store, scan_people_directory(store))

        assert index.all_person_ids() == {"homer-simpson"}


class TestDeleteAndRebuildEquivalence:
    def test_deleting_and_rebuilding_produces_equivalent_index(self, tmp_path):
        store = _store(tmp_path)
        store.write_person(_homer())
        store.write_person(_no_year_birthday_person())
        store.write_person(
            Person(id="lisa-simpson", name="Lisa Simpson", facts=[Fact(id="fact-1", category="Interests", text="Sax.")])
        )
        db_path = tmp_path / "data" / "index" / "relationships.sqlite"

        index = SqliteIndex(db_path)
        index.rebuild_from_scan(store, scan_people_directory(store))
        rows_before = sorted((_drop_updated_at(r) for r in index.list_all()), key=lambda r: r["person_id"])
        index.close()

        db_path.unlink()  # simulate `montauk rebuild-index` after deleting the derived db
        (db_path.parent / f"{db_path.name}-wal").unlink(missing_ok=True)
        (db_path.parent / f"{db_path.name}-shm").unlink(missing_ok=True)

        index2 = SqliteIndex(db_path)
        index2.rebuild_from_scan(store, scan_people_directory(store))
        rows_after = sorted((_drop_updated_at(r) for r in index2.list_all()), key=lambda r: r["person_id"])
        index2.close()

        assert rows_before == rows_after
        assert len(rows_after) == 3


class TestIncrementalReconcile:
    def test_new_file_is_inserted(self, tmp_path):
        store = _store(tmp_path)
        index = SqliteIndex(tmp_path / "data" / "index" / "relationships.sqlite")
        index.rebuild_from_scan(store, scan_people_directory(store))

        store.write_person(_homer())
        stats = index.reconcile(store, scan_people_directory(store))

        assert stats.inserted == 1
        assert stats.updated == 0
        assert stats.unchanged == 0
        assert stats.removed == 0
        assert index.all_person_ids() == {"homer-simpson"}

    def test_unchanged_file_is_not_rewritten(self, tmp_path):
        store = _store(tmp_path)
        store.write_person(_homer())
        index = SqliteIndex(tmp_path / "data" / "index" / "relationships.sqlite")
        index.rebuild_from_scan(store, scan_people_directory(store))

        stats = index.reconcile(store, scan_people_directory(store))

        assert stats.inserted == 0
        assert stats.updated == 0
        assert stats.unchanged == 1
        assert stats.removed == 0

    def test_changed_file_is_updated(self, tmp_path):
        store = _store(tmp_path)
        store.write_person(_homer())
        index = SqliteIndex(tmp_path / "data" / "index" / "relationships.sqlite")
        index.rebuild_from_scan(store, scan_people_directory(store))

        time.sleep(0.01)
        updated_homer = _homer().model_copy(update={"summary": "Updated summary."})
        store.write_person(updated_homer)
        stats = index.reconcile(store, scan_people_directory(store))

        assert stats.updated == 1
        assert stats.unchanged == 0
        assert index.get_row("homer-simpson")["summary"] == "Updated summary."

    def test_archived_person_is_removed_from_index(self, tmp_path):
        store = _store(tmp_path)
        store.write_person(_homer())
        index = SqliteIndex(tmp_path / "data" / "index" / "relationships.sqlite")
        index.rebuild_from_scan(store, scan_people_directory(store))

        store.archive_person("homer-simpson")
        stats = index.reconcile(store, scan_people_directory(store))

        assert stats.removed == 1
        assert index.all_person_ids() == set()


class TestMeta:
    def test_meta_round_trips(self, tmp_path):
        index = SqliteIndex(tmp_path / "relationships.sqlite")
        assert index.get_meta("schema_version") is None
        index.set_meta("schema_version", "1")
        assert index.get_meta("schema_version") == "1"
        index.set_meta("schema_version", "2")
        assert index.get_meta("schema_version") == "2"

    def test_rebuild_sets_reconciliation_meta(self, tmp_path):
        store = _store(tmp_path)
        index = SqliteIndex(tmp_path / "data" / "index" / "relationships.sqlite")
        index.rebuild_from_scan(store, scan_people_directory(store))
        assert index.get_meta("last_reconciliation_at") is not None
        assert index.get_meta("schema_version") == "1"
