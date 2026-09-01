"""Clean cutover from name-derived person IDs to generic IDs."""

import pytest

from montauk.ids import PERSON_ID_RE
from montauk.markdown_store import MarkdownStore
from montauk.migration import MigrationError, migrate_person_ids
from montauk.reconciliation import scan_people_directory

HOMER = """---
id: homer-simpson
name: Homer Simpson
aliases:
  - Homer J. Simpson
---

# Homer Simpson

## Family

- id: fact-1
  confidence: high
  text: Married to Marge.
  related_person_id: marge-simpson

## Work & Education

## Interests

## Relationship with User

## Life Events

## General Notes

## Interactions

### int-1
- date: 2026-01-01
- summary: Talked over the fence.
"""

MARGE = """---
id: marge-simpson
name: Marge Simpson
---

# Marge Simpson

## Family

- id: fact-1
  confidence: high
  text: Married to Homer.
  related_person_id: homer-simpson

## Work & Education

## Interests

## Relationship with User

## Life Events

## General Notes

## Interactions
"""

FRANK_ARCHIVED = """---
id: frank-grimes
name: Frank Grimes
---

# Frank Grimes

## Family

## Work & Education

## Interests

## Relationship with User

## Life Events

## General Notes

## Interactions
"""


def _legacy_store(tmp_path) -> MarkdownStore:
    store = MarkdownStore(tmp_path / "data")
    (store.people_dir / "homer-simpson.md").write_text(HOMER, encoding="utf-8")
    (store.people_dir / "marge-simpson.md").write_text(MARGE, encoding="utf-8")
    (store.archive_dir / "frank-grimes.md").write_text(FRANK_ARCHIVED, encoding="utf-8")
    return store


class TestMigration:
    def test_assigns_one_generic_id_per_person_in_deterministic_order(self, tmp_path):
        store = _legacy_store(tmp_path)
        report = migrate_person_ids(store, make_backup=False)

        # active files first (lexical), then archive.
        assert report.migrated == {
            "homer-simpson": "P0001",
            "marge-simpson": "P0002",
            "frank-grimes": "P0003",
        }
        assert sorted(store.list_person_ids()) == ["P0001", "P0002"]
        assert store.list_archived_person_ids() == ["P0003"]

    def test_rewrites_relationship_references(self, tmp_path):
        store = _legacy_store(tmp_path)
        migrate_person_ids(store, make_backup=False)

        homer = store.read_person("P0001")
        marge = store.read_person("P0002")
        assert homer.facts[0].related_person_id == "P0002"
        assert marge.facts[0].related_person_id == "P0001"

    def test_preserves_all_content_and_local_ids(self, tmp_path):
        store = _legacy_store(tmp_path)
        migrate_person_ids(store, make_backup=False)
        homer = store.read_person("P0001")
        assert homer.name == "Homer Simpson"
        assert homer.aliases == ["Homer J. Simpson"]
        assert [f.text for f in homer.facts] == ["Married to Marge."]
        assert [i.id for i in homer.interactions] == ["int-1"]
        assert homer.interactions[0].summary == "Talked over the fence."

    def test_scan_is_healthy_and_only_generic_ids_remain(self, tmp_path):
        store = _legacy_store(tmp_path)
        report = migrate_person_ids(store, make_backup=False)
        assert report.verified is True

        scan = scan_people_directory(store)
        assert scan.healthy is True
        assert all(PERSON_ID_RE.match(pid) for pid in scan.valid)
        assert scan.warnings == []  # filenames match ids

    def test_advances_allocator_so_next_person_is_p0004(self, tmp_path):
        store = _legacy_store(tmp_path)
        migrate_person_ids(store, make_backup=False)
        assert store.id_sequence.allocate() == "P0004"

    def test_is_idempotent(self, tmp_path):
        store = _legacy_store(tmp_path)
        first = migrate_person_ids(store, make_backup=False)
        second = migrate_person_ids(store, make_backup=False)
        assert first.migrated
        assert second.migrated == {}
        assert second.already_generic == ["P0001", "P0002", "P0003"]
        assert sorted(store.list_person_ids()) == ["P0001", "P0002"]

    def test_resumes_after_a_partial_migration(self, tmp_path):
        store = _legacy_store(tmp_path)
        migrate_person_ids(store, make_backup=False)
        # Simulate a straggler legacy file dropped back in afterwards.
        (store.people_dir / "lenny-leonard.md").write_text(
            "---\nid: lenny-leonard\nname: Lenny\n---\n\n# Lenny\n\n## Interactions\n", encoding="utf-8"
        )
        report = migrate_person_ids(store, make_backup=False)
        assert report.migrated == {"lenny-leonard": "P0004"}
        assert store.read_person("P0004").name == "Lenny"

    def test_aborts_when_a_file_cannot_be_parsed_for_its_id(self, tmp_path):
        store = _legacy_store(tmp_path)
        (store.people_dir / "broken.md").write_text("no front matter here\n", encoding="utf-8")
        with pytest.raises(MigrationError):
            migrate_person_ids(store, make_backup=False)
        # Nothing was rewritten.
        assert store.people_dir.joinpath("homer-simpson.md").exists()

    def test_no_op_on_an_already_generic_repository(self, tmp_path):
        store = MarkdownStore(tmp_path / "data")
        (store.people_dir / "P0001.md").write_text(
            "---\nid: P0001\nname: Already Generic\n---\n\n# Already Generic\n\n## Interactions\n",
            encoding="utf-8",
        )
        report = migrate_person_ids(store, make_backup=False)
        assert report.migrated == {}
        assert store.id_sequence.allocate() == "P0002"
