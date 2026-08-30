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


def _store(tmp_path: Path) -> MarkdownStore:
    return MarkdownStore(tmp_path / "data")


def _write_raw(store: MarkdownStore, filename: str, content: str) -> None:
    (store.people_dir / filename).write_text(content, encoding="utf-8")


class TestScanHappyPath:
    def test_all_valid_files_are_healthy(self, tmp_path):
        store = _store(tmp_path)
        store.write_person(Person(id="bart-simpson", name="Bart Simpson"))
        store.write_person(Person(id="lisa-simpson", name="Lisa Simpson"))

        result = scan_people_directory(store)

        assert result.healthy is True
        assert set(result.valid) == {"bart-simpson", "lisa-simpson"}
        assert result.issues == []

    def test_empty_directory_is_healthy(self, tmp_path):
        result = scan_people_directory(_store(tmp_path))
        assert result.healthy is True
        assert result.valid == {}


class TestScanSkipsMalformedFiles:
    def test_malformed_file_is_skipped_with_specific_error(self, tmp_path):
        store = _store(tmp_path)
        store.write_person(Person(id="lisa-simpson", name="Lisa Simpson"))
        barney_content = (SIMPSONS_PEOPLE_DIR / "barney-gumble.md").read_text()
        _write_raw(store, "barney-gumble.md", barney_content)

        result = scan_people_directory(store)

        assert result.healthy is False
        assert "lisa-simpson" in result.valid
        assert "barney-gumble" not in result.valid
        assert len(result.errors) == 1
        error = result.errors[0]
        assert error.error_type == "format_error"
        assert "barney-gumble.md" in error.file_path
        assert "line" in error.message

    def test_healthy_records_still_served_alongside_a_broken_one(self, tmp_path):
        store = _store(tmp_path)
        for name in ("homer-simpson", "marge-simpson", "ned-flanders"):
            content = (SIMPSONS_PEOPLE_DIR / f"{name}.md").read_text()
            _write_raw(store, f"{name}.md", content)
        _write_raw(store, "barney-gumble.md", (SIMPSONS_PEOPLE_DIR / "barney-gumble.md").read_text())

        result = scan_people_directory(store)

        assert result.healthy is False
        assert set(result.valid) == {"homer-simpson", "marge-simpson", "ned-flanders"}

    def test_semantically_invalid_file_is_skipped(self, tmp_path):
        store = _store(tmp_path)
        bad = (
            "---\nid: bad-cadence\nname: Bad Cadence\n"
            "desired_contact_cadence_days: -5\n---\n\n# Bad Cadence\n\n## Interactions\n"
        )
        _write_raw(store, "bad-cadence.md", bad)

        result = scan_people_directory(store)

        assert result.healthy is False
        assert result.errors[0].error_type == "validation_error"


class TestFilenameIdMismatch:
    def test_mismatch_is_a_warning_not_an_error(self, tmp_path):
        store = _store(tmp_path)
        content = "---\nid: actual-id\nname: Someone\n---\n\n# Someone\n\n## Interactions\n"
        _write_raw(store, "wrong-filename.md", content)

        result = scan_people_directory(store)

        assert result.healthy is True  # warnings don't degrade health
        assert "actual-id" in result.valid  # front-matter id is authoritative
        assert len(result.warnings) == 1
        assert result.warnings[0].error_type == "filename_id_mismatch"


class TestDuplicatePersonId:
    def test_second_file_claiming_an_id_already_taken_is_excluded(self, tmp_path):
        store = _store(tmp_path)
        content_a = "---\nid: shared-id\nname: First Claimant\n---\n\n# First Claimant\n\n## Interactions\n"
        content_b = "---\nid: shared-id\nname: Second Claimant\n---\n\n# Second Claimant\n\n## Interactions\n"
        _write_raw(store, "a-file.md", content_a)
        _write_raw(store, "b-file.md", content_b)

        result = scan_people_directory(store)

        assert result.healthy is False
        assert result.valid["shared-id"].name == "First Claimant"
        dup_errors = [i for i in result.errors if i.error_type == "duplicate_person_id"]
        assert len(dup_errors) == 1
        assert "b-file.md" in dup_errors[0].file_path


class TestDanglingRelatedPersonId:
    def test_dangling_reference_is_a_warning_and_person_stays_valid(self, tmp_path):
        store = _store(tmp_path)
        store.write_person(
            Person(
                id="homer-simpson",
                name="Homer Simpson",
                facts=[
                    Fact(
                        id="fact-1",
                        category="Family",
                        text="Married to someone not in the repo yet.",
                        related_person_id="ghost-person",
                    )
                ],
            )
        )

        result = scan_people_directory(store)

        assert result.healthy is True
        assert "homer-simpson" in result.valid
        assert len(result.warnings) == 1
        assert result.warnings[0].error_type == "dangling_related_person_id"

    def test_resolved_reference_produces_no_warning(self, tmp_path):
        store = _store(tmp_path)
        store.write_person(Person(id="marge-simpson", name="Marge Simpson"))
        store.write_person(
            Person(
                id="homer-simpson",
                name="Homer Simpson",
                facts=[
                    Fact(
                        id="fact-1",
                        category="Family",
                        text="Married to Marge.",
                        related_person_id="marge-simpson",
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
        store.write_person(Person(id="lisa-simpson", name="Lisa Simpson"))
        _write_raw(store, "barney-gumble.md", (SIMPSONS_PEOPLE_DIR / "barney-gumble.md").read_text())
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
        store.write_person(Person(id="lisa-simpson", name="Lisa Simpson"))
        result = scan_people_directory(store)

        path = write_validation_report(result, store.data_dir)
        report = json.loads(path.read_text())

        assert report["healthy"] is True
        assert report["issues"] == []
