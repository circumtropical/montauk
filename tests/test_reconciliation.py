import json
from pathlib import Path

from montauk.markdown_store import MarkdownStore
from montauk.models import Fact, Person
from montauk.reconciliation import (
    scan_people_directory,
    validation_report_path,
    write_validation_report,
)

SIMPSONS_PEOPLE_DIR = Path(__file__).parent.parent / "examples" / "simpsons" / "people"
# P0001 is the deliberately-malformed fixture file (Barney Gumble).
BARNEY_MALFORMED = (SIMPSONS_PEOPLE_DIR / "P0001.md").read_text()


def _store(tmp_path: Path) -> MarkdownStore:
    return MarkdownStore(tmp_path / "data")


def _write_raw(store: MarkdownStore, filename: str, content: str) -> None:
    (store.people_dir / filename).write_text(content, encoding="utf-8")


class TestScanHappyPath:
    def test_all_valid_files_are_healthy(self, tmp_path):
        store = _store(tmp_path)
        store.write_person(Person(id="P0001", name="Bart Simpson"))
        store.write_person(Person(id="P0002", name="Lisa Simpson"))

        result = scan_people_directory(store)

        assert result.healthy is True
        assert set(result.valid) == {"P0001", "P0002"}
        assert result.issues == []

    def test_empty_directory_is_healthy(self, tmp_path):
        result = scan_people_directory(_store(tmp_path))
        assert result.healthy is True
        assert result.valid == {}


class TestScanSkipsMalformedFiles:
    def test_malformed_file_is_skipped_with_specific_error(self, tmp_path):
        store = _store(tmp_path)
        store.write_person(Person(id="P0002", name="Lisa Simpson"))
        _write_raw(store, "P0001.md", BARNEY_MALFORMED)

        result = scan_people_directory(store)

        assert result.healthy is False
        assert "P0002" in result.valid
        assert "P0001" not in result.valid
        assert len(result.errors) == 1
        error = result.errors[0]
        assert error.error_type == "format_error"
        assert "P0001.md" in error.file_path
        assert "line" in error.message

    def test_healthy_records_still_served_alongside_a_broken_one(self, tmp_path):
        store = _store(tmp_path)
        for stem in ("P0004", "P0005", "P0008"):  # homer, marge, ned
            _write_raw(store, f"{stem}.md", (SIMPSONS_PEOPLE_DIR / f"{stem}.md").read_text())
        _write_raw(store, "P0001.md", BARNEY_MALFORMED)

        result = scan_people_directory(store)

        assert result.healthy is False
        assert set(result.valid) == {"P0004", "P0005", "P0008"}

    def test_semantically_invalid_file_is_skipped(self, tmp_path):
        store = _store(tmp_path)
        bad = (
            "---\nid: P0007\nname: Bad Cadence\n"
            "desired_contact_cadence_days: -5\n---\n\n# Bad Cadence\n\n## Interactions\n"
        )
        _write_raw(store, "P0007.md", bad)

        result = scan_people_directory(store)

        assert result.healthy is False
        assert result.errors[0].error_type == "validation_error"

    def test_name_derived_id_is_rejected_as_invalid(self, tmp_path):
        # After the generic-ID cutover, a legacy name-derived id must fail
        # normal validation -- it is not silently resolved.
        store = _store(tmp_path)
        _write_raw(
            store,
            "mike-chen.md",
            "---\nid: mike-chen\nname: Mike Chen\n---\n\n# Mike Chen\n\n## Interactions\n",
        )

        result = scan_people_directory(store)

        assert result.healthy is False
        assert result.errors[0].error_type == "validation_error"


class TestFilenameIdMismatch:
    def test_mismatch_is_a_warning_not_an_error(self, tmp_path):
        store = _store(tmp_path)
        content = "---\nid: P0042\nname: Someone\n---\n\n# Someone\n\n## Interactions\n"
        _write_raw(store, "P0001.md", content)

        result = scan_people_directory(store)

        assert result.healthy is True  # warnings don't degrade health
        assert "P0042" in result.valid  # front-matter id is authoritative
        assert len(result.warnings) == 1
        assert result.warnings[0].error_type == "filename_id_mismatch"


class TestDuplicatePersonId:
    def test_second_file_claiming_an_id_already_taken_is_excluded(self, tmp_path):
        store = _store(tmp_path)
        content_a = "---\nid: P0009\nname: First Claimant\n---\n\n# First Claimant\n\n## Interactions\n"
        content_b = "---\nid: P0009\nname: Second Claimant\n---\n\n# Second Claimant\n\n## Interactions\n"
        _write_raw(store, "a-file.md", content_a)
        _write_raw(store, "b-file.md", content_b)

        result = scan_people_directory(store)

        assert result.healthy is False
        assert result.valid["P0009"].name == "First Claimant"
        dup_errors = [i for i in result.errors if i.error_type == "duplicate_person_id"]
        assert len(dup_errors) == 1
        assert "b-file.md" in dup_errors[0].file_path


class TestDanglingRelatedPersonId:
    def test_dangling_reference_is_a_warning_and_person_stays_valid(self, tmp_path):
        store = _store(tmp_path)
        store.write_person(
            Person(
                id="P0004",
                name="Homer Simpson",
                facts=[
                    Fact(
                        id="fact-1",
                        category="Family",
                        text="Married to someone not in the repo yet.",
                        related_person_id="P0099",
                    )
                ],
            )
        )

        result = scan_people_directory(store)

        assert result.healthy is True
        assert "P0004" in result.valid
        assert len(result.warnings) == 1
        assert result.warnings[0].error_type == "dangling_related_person_id"

    def test_resolved_reference_produces_no_warning(self, tmp_path):
        store = _store(tmp_path)
        store.write_person(Person(id="P0005", name="Marge Simpson"))
        store.write_person(
            Person(
                id="P0004",
                name="Homer Simpson",
                facts=[
                    Fact(
                        id="fact-1",
                        category="Family",
                        text="Married to Marge.",
                        related_person_id="P0005",
                    )
                ],
            )
        )

        result = scan_people_directory(store)

        assert result.healthy is True
        assert result.warnings == []


class TestValidationReport:
    def test_writes_valid_json_report(self, tmp_path):
        store = _store(tmp_path)
        store.write_person(Person(id="P0002", name="Lisa Simpson"))
        _write_raw(store, "P0001.md", BARNEY_MALFORMED)
        result = scan_people_directory(store)

        path = write_validation_report(result, store.data_dir)

        assert path == validation_report_path(store.data_dir)
        report = json.loads(path.read_text())
        assert report["healthy"] is False
        assert report["valid_person_count"] == 1
        assert report["error_count"] == 1
        assert report["warning_count"] == 0
        assert len(report["issues"]) == 1
        assert report["issues"][0]["error_type"] == "format_error"

    def test_report_reflects_healthy_repository(self, tmp_path):
        store = _store(tmp_path)
        store.write_person(Person(id="P0002", name="Lisa Simpson"))
        result = scan_people_directory(store)

        path = write_validation_report(result, store.data_dir)
        report = json.loads(path.read_text())

        assert report["healthy"] is True
        assert report["issues"] == []
