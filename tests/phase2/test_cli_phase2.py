"""`montauk db ...` and `montauk migrate phase2 ...` CLI (spec 30.4, 33)."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from montauk.cli import app

from ._phase1_fixtures import write_golden

runner = CliRunner()


@pytest.fixture
def _db_env(monkeypatch, database_url, engine, _truncate):
    monkeypatch.setenv("MONTAUK_DATABASE_URL", database_url)


def test_db_current_reports_up_to_date(_db_env):
    result = runner.invoke(app, ["db", "current"])
    assert result.exit_code == 0
    assert "up to date" in result.stdout


def test_migrate_phase2_dry_run_then_execute(_db_env, tmp_path):
    src = write_golden(tmp_path / "p1")

    dry = runner.invoke(
        app,
        ["migrate", "phase2", "--source-data-dir", str(src), "--workspace", "Family"],
    )
    assert dry.exit_code == 0, dry.stdout
    assert "dry_run_ok" in dry.stdout
    assert "active people:     3" in dry.stdout

    ex = runner.invoke(
        app,
        [
            "migrate",
            "phase2",
            "--source-data-dir",
            str(src),
            "--workspace",
            "Family",
            "--execute",
            "--no-backup",
            "--json",
        ],
    )
    assert ex.exit_code == 0, ex.stdout
    report = json.loads(ex.stdout)
    assert report["status"] == "succeeded"
    assert report["counts"]["active_people"] == 3
    assert report["hash_mismatches"] == []

    verified = runner.invoke(app, ["migrate", "verify", "--migration-id", report["run_id"], "--json"])
    assert verified.exit_code == 0
    assert json.loads(verified.stdout)["status"] == "succeeded"


def test_migrate_phase2_execute_with_malformed_file_exits_nonzero(_db_env, tmp_path):
    src = write_golden(tmp_path / "p1")
    (src / "people" / "P0002.md").write_text("---\nid: P0002\nname: [bad\n---\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "migrate",
            "phase2",
            "--source-data-dir",
            str(src),
            "--workspace",
            "Family",
            "--execute",
            "--no-backup",
        ],
    )
    assert result.exit_code == 1
    assert "aborted_blocking_errors" in result.stdout


def test_migrate_without_database_url_is_a_clear_error(monkeypatch, tmp_path):
    monkeypatch.delenv("MONTAUK_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    src = write_golden(tmp_path / "p1")
    result = runner.invoke(app, ["migrate", "phase2", "--source-data-dir", str(src), "--workspace", "X"])
    assert result.exit_code == 2
    assert "no PostgreSQL DSN configured" in result.output
