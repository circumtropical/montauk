"""Phase 1 -> Phase 2 migration golden tests (spec 30, 32.6)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from sqlalchemy import func, select

from montauk.db import models as orm
from montauk.db.mapping import person_to_domain
from montauk.db.repositories import Actor, PeopleRepository, WorkspaceScope
from montauk.exporters.json_export import person_to_dict
from montauk.exporters.markdown import person_to_markdown
from montauk.markdown_store import markdown_to_person
from montauk.phase2_migration import (
    MigrationError,
    load_source,
    run_migration,
    verify_migration,
)
from montauk.services.workspace import get_workspace_by_slug

from ._phase1_fixtures import write_golden, write_phase1


def _tree_snapshot(root: Path) -> dict[str, str]:
    out = {}
    for f in sorted(root.rglob("*")):
        if f.is_file():
            out[str(f.relative_to(root))] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out


@pytest.fixture
def golden(tmp_path: Path) -> Path:
    return write_golden(tmp_path / "phase1")


def _scope(session, slug: str = "montauk") -> WorkspaceScope:
    ws = get_workspace_by_slug(session, slug)
    assert ws is not None
    return WorkspaceScope(session, ws.id, Actor("owner"))


class TestDryRun:
    def test_dry_run_mutates_nothing(self, session_maker, golden, engine, _truncate):
        before = _tree_snapshot(golden)
        report = run_migration(session_maker, source_dir=golden, workspace_name="Montauk", mode="dry_run")
        assert report.status == "dry_run_ok"
        assert _tree_snapshot(golden) == before

        with session_maker() as s:
            assert s.execute(select(func.count(orm.Person.id))).scalar_one() == 0
            assert s.execute(select(func.count(orm.Workspace.id))).scalar_one() == 0

    def test_dry_run_reports_counts_and_ids(self, session_maker, golden, engine, _truncate):
        report = run_migration(session_maker, source_dir=golden, workspace_name="Montauk", mode="dry_run")
        assert report.person_ids == ["P0001", "P0002", "P0003"]
        assert report.archived_person_ids == ["P0009"]
        assert report.counts["facts"] == 6 + 1 + 3 + 2
        assert report.counts["interactions"] == 2 + 1 + 1 + 1
        assert report.counts["relationships"] == 2
        assert report.source_high_water == 40
        assert report.destination_high_water == 40  # gaps -> hwm stays at the sequence value

    def test_dry_run_flags_dangling_reference_as_warning_not_error(
        self, session_maker, tmp_path, engine, _truncate
    ):
        people = {
            "P0001": (
                "---\nid: P0001\nname: A\n---\n\n# A\n\n## Family\n\n"
                "- id: fact-1\n  confidence: high\n  text: friend of B\n"
                "  related_person_id: P0404\n\n## Work & Education\n\n## Interests\n\n"
                "## Relationship with User\n\n## Life Events\n\n## General Notes\n\n## Interactions\n"
            )
        }
        root = write_phase1(tmp_path / "p1", people=people, archived={}, high_water=1)
        report = run_migration(session_maker, source_dir=root, workspace_name="WS", mode="dry_run")
        assert report.status == "dry_run_ok"
        assert len(report.dangling_references) == 1
        assert not report.blocking_errors


class TestExecute:
    def test_migrates_people_ids_and_archive_state(self, session_maker, golden, engine, _truncate):
        report = run_migration(session_maker, source_dir=golden, workspace_name="Montauk", mode="execute")
        assert report.status == "succeeded"

        with session_maker() as s:
            scope = _scope(s)
            repo = PeopleRepository(scope)
            assert {p.public_id for p in repo.list_people(archived=False)} == {
                "P0001",
                "P0002",
                "P0003",
            }
            assert {p.public_id for p in repo.list_people(archived=True)} == {"P0009"}

    def test_source_bytes_and_git_state_unchanged_after_execute(
        self, session_maker, golden, engine, _truncate
    ):
        import subprocess

        subprocess.run(["git", "init", "-q"], cwd=golden, check=True)
        subprocess.run(["git", "add", "-A"], cwd=golden, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
            cwd=golden,
            check=True,
        )
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=golden, capture_output=True, text=True).stdout
        before = _tree_snapshot(golden / "people"), _tree_snapshot(golden / "archive")

        run_migration(
            session_maker,
            source_dir=golden,
            workspace_name="Montauk",
            mode="execute",
            make_backup=False,
        )

        assert (_tree_snapshot(golden / "people"), _tree_snapshot(golden / "archive")) == before
        after_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=golden, capture_output=True, text=True
        ).stdout
        assert head == after_head
        assert (
            subprocess.run(
                ["git", "status", "--porcelain"], cwd=golden, capture_output=True, text=True
            ).stdout
            == ""
        )

    def test_backup_is_a_copy_outside_the_source_tree(
        self, session_maker, golden, tmp_path, engine, _truncate
    ):
        backup = tmp_path / "backups" / "run1"
        report = run_migration(
            session_maker,
            source_dir=golden,
            workspace_name="Montauk",
            mode="execute",
            backup_dir=backup,
        )
        assert report.backup_status == "created"
        assert (backup / "people" / "P0001.md").read_bytes() == (golden / "people" / "P0001.md").read_bytes()
        assert (backup / "archive" / "P0009.md").exists()
        assert (backup / "person-id-sequence.json").exists()

    def test_all_valid_values_round_trip_after_normalization(self, session_maker, golden, engine, _truncate):
        run_migration(session_maker, source_dir=golden, workspace_name="Montauk", mode="execute")
        with session_maker() as s:
            repo = PeopleRepository(_scope(s))
            for pid in ("P0001", "P0002", "P0003", "P0009"):
                src_file = (golden / ("archive" if pid == "P0009" else "people") / f"{pid}.md").read_text()
                src_canonical = person_to_markdown(markdown_to_person(src_file))
                dest_canonical = person_to_markdown(
                    person_to_domain(repo.require(pid, include_archived=True))
                )
                assert dest_canonical == src_canonical

    def test_per_person_hash_report_reconciles(self, session_maker, golden, engine, _truncate):
        report = run_migration(session_maker, source_dir=golden, workspace_name="Montauk", mode="execute")
        assert report.hash_mismatches == []
        assert not report.lossy
        assert len(report.per_person_hash) == 4
        assert all(row["match"] for row in report.per_person_hash)

    def test_relationships_to_active_and_archived_people_resolve(
        self, session_maker, golden, engine, _truncate
    ):
        run_migration(session_maker, source_dir=golden, workspace_name="Montauk", mode="execute")
        with session_maker() as s:
            repo = PeopleRepository(_scope(s))
            marco = repo.require("P0003")
            by_local = {f.local_id: f for f in marco.facts}
            assert by_local["fact-1"].related_person_public_id == "P0001"
            assert by_local["fact-1"].related_person_id is not None  # active
            assert by_local["fact-3"].related_person_public_id == "P0009"
            assert by_local["fact-3"].related_person_id is not None  # archived

    def test_future_ids_start_above_source_high_water(self, session_maker, tmp_path, engine, _truncate):
        # source sequence says 40 but the highest actual id is P0009
        root = write_golden(tmp_path / "p1", high_water=40)
        run_migration(session_maker, source_dir=root, workspace_name="WS", mode="execute")
        with session_maker() as s:
            repo = PeopleRepository(_scope(s, "ws"))
            assert repo.allocate_public_id() == "P0041"

    def test_high_water_advances_past_ids_even_if_sequence_is_behind(
        self, session_maker, tmp_path, engine, _truncate
    ):
        root = write_golden(tmp_path / "p1", high_water=3)  # behind P0009
        run_migration(session_maker, source_dir=root, workspace_name="WS", mode="execute")
        with session_maker() as s:
            repo = PeopleRepository(_scope(s, "ws"))
            assert repo.allocate_public_id() == "P0010"

    def test_markdown_and_json_export_are_semantically_equivalent_to_source(
        self, session_maker, golden, engine, _truncate
    ):
        run_migration(session_maker, source_dir=golden, workspace_name="Montauk", mode="execute")
        with session_maker() as s:
            repo = PeopleRepository(_scope(s))
            src = markdown_to_person((golden / "people" / "P0001.md").read_text())
            migrated = person_to_domain(repo.require("P0001"))
            assert person_to_markdown(migrated) == person_to_markdown(src)
            assert person_to_dict(migrated) == person_to_dict(src)


class TestMalformedRecords:
    def _write_with_bad_file(self, tmp_path: Path, bad_body: str, name: str = "P0002") -> Path:
        root = write_golden(tmp_path / "p1")
        (root / "people" / f"{name}.md").write_text(bad_body, encoding="utf-8")
        return root

    def test_malformed_yaml_blocks_cutover_and_is_not_skipped(
        self, session_maker, tmp_path, engine, _truncate
    ):
        root = self._write_with_bad_file(
            tmp_path, "---\nid: P0002\nname: [unterminated\n---\n\n# x\n\n## Interactions\n"
        )
        report = run_migration(session_maker, source_dir=root, workspace_name="WS", mode="execute")
        assert report.status == "aborted_blocking_errors"
        assert any(e["error_type"] == "format_error" for e in report.blocking_errors)
        with session_maker() as s:
            assert s.execute(select(func.count(orm.Person.id))).scalar_one() == 0

    def test_unknown_category_blocks_cutover(self, session_maker, tmp_path, engine, _truncate):
        root = self._write_with_bad_file(
            tmp_path,
            "---\nid: P0002\nname: Bad Cat\n---\n\n# Bad Cat\n\n## Nonsense Category\n\n"
            "- id: fact-1\n  confidence: high\n  text: x\n\n## Interactions\n",
        )
        report = run_migration(session_maker, source_dir=root, workspace_name="WS", mode="execute")
        assert report.status == "aborted_blocking_errors"

    def test_duplicate_local_fact_ids_block_cutover(self, session_maker, tmp_path, engine, _truncate):
        root = self._write_with_bad_file(
            tmp_path,
            "---\nid: P0002\nname: Dup\n---\n\n# Dup\n\n## Family\n\n"
            "- id: fact-1\n  confidence: high\n  text: a\n"
            "- id: fact-1\n  confidence: high\n  text: b\n\n## Interactions\n",
        )
        report = run_migration(session_maker, source_dir=root, workspace_name="WS", mode="execute")
        assert report.status == "aborted_blocking_errors"

    def test_filename_id_mismatch_is_a_warning_not_a_block(self, session_maker, tmp_path, engine, _truncate):
        root = write_golden(tmp_path / "p1")
        (root / "people" / "P0001.md").rename(root / "people" / "P0055.md")
        report = run_migration(session_maker, source_dir=root, workspace_name="WS", mode="execute")
        assert report.status == "succeeded"
        assert any(w["error_type"] == "filename_id_mismatch" for w in report.warnings)

    def test_duplicate_person_id_across_files_blocks(self, session_maker, tmp_path, engine, _truncate):
        root = write_golden(tmp_path / "p1")
        (root / "people" / "P0003.md").write_text(
            (root / "people" / "P0001.md").read_text(), encoding="utf-8"
        )
        report = run_migration(session_maker, source_dir=root, workspace_name="WS", mode="execute")
        assert report.status == "aborted_blocking_errors"


class TestFaultInjectionAndRerun:
    def test_injected_failure_rolls_the_whole_import_back(self, session_maker, golden, engine, _truncate):
        def boom() -> None:
            raise RuntimeError("disk full mid-import")

        with pytest.raises(MigrationError):
            run_migration(
                session_maker,
                source_dir=golden,
                workspace_name="Montauk",
                mode="execute",
                make_backup=False,
                fault_injection=boom,
            )
        with session_maker() as s:
            assert s.execute(select(func.count(orm.Person.id))).scalar_one() == 0
            # a failed run is recorded, but no workspace data
            runs = s.execute(select(orm.LegacyMigrationRun)).scalars().all()
            assert [r.status for r in runs] == ["failed"]

    def test_rerun_after_success_is_refused_not_duplicated(self, session_maker, golden, engine, _truncate):
        first = run_migration(session_maker, source_dir=golden, workspace_name="Montauk", mode="execute")
        assert first.status == "succeeded"
        second = run_migration(
            session_maker,
            source_dir=golden,
            workspace_name="Montauk",
            mode="execute",
            make_backup=False,
        )
        assert second.status == "already_migrated"
        with session_maker() as s:
            assert s.execute(select(func.count(orm.Person.id))).scalar_one() == 4

    def test_execute_into_a_non_empty_workspace_is_refused(
        self, session_maker, golden, tmp_path, engine, _truncate
    ):
        # pre-populate the workspace out of band
        from montauk.services.workspace import get_or_create_workspace

        with session_maker() as s:
            ws = get_or_create_workspace(s, "Montauk")
            repo = PeopleRepository(WorkspaceScope(s, ws.id, Actor("owner")))
            from montauk.models import Person

            repo.create(Person(id="P0001", name="Pre-existing"))
            s.commit()

        report = run_migration(
            session_maker,
            source_dir=golden,
            workspace_name="Montauk",
            mode="execute",
            make_backup=False,
        )
        assert report.status == "destination_not_empty"
        with session_maker() as s:
            assert s.execute(select(func.count(orm.Person.id))).scalar_one() == 1

    def test_verify_migration_reads_back_the_stored_report(self, session_maker, golden, engine, _truncate):
        report = run_migration(session_maker, source_dir=golden, workspace_name="Montauk", mode="execute")
        with session_maker() as s:
            verified = verify_migration(s, run_id=report.run_id)
        assert verified["status"] == "succeeded"
        assert verified["report"]["counts"]["facts"] == 12
        assert verified["source_manifest_hash"] == report.source_manifest_hash


class TestSourceInspection:
    def test_load_source_separates_active_and_archived(self, golden):
        staged, issues, hwm = load_source(golden)
        assert hwm == 40
        assert {s.person.id for s in staged if not s.archived} == {"P0001", "P0002", "P0003"}
        assert {s.person.id for s in staged if s.archived} == {"P0009"}
        assert [i for i in issues if i.severity == "error"] == []

    def test_missing_birthday_year_is_preserved(self, golden):
        staged, _, _ = load_source(golden)
        marco = next(s.person for s in staged if s.person.id == "P0003")
        assert marco.birthday.to_string() == "03-14"
        assert marco.birthday.year is None
