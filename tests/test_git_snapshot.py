import datetime as dt
import subprocess
from pathlib import Path

import pytest

from montauk.git_snapshot import DailySnapshotScheduler, ensure_git_repo, snapshot_if_changed


def _git_log(data_dir: Path) -> list[str]:
    result = subprocess.run(
        ["git", "log", "--format=%s"], cwd=data_dir, capture_output=True, text=True, check=True
    )
    return [line for line in result.stdout.splitlines() if line]


def _write_person_file(data_dir: Path, name: str, content: str) -> None:
    people_dir = data_dir / "people"
    people_dir.mkdir(parents=True, exist_ok=True)
    (people_dir / name).write_text(content, encoding="utf-8")


class TestEnsureGitRepo:
    def test_initializes_git_repo(self, tmp_path):
        data_dir = tmp_path / "data"
        ensure_git_repo(data_dir)
        assert (data_dir / ".git").is_dir()

    def test_writes_gitignore_excluding_derived_and_secret_dirs(self, tmp_path):
        data_dir = tmp_path / "data"
        ensure_git_repo(data_dir)
        gitignore = (data_dir / ".gitignore").read_text()
        assert "index/" in gitignore
        assert "auth/" in gitignore

    def test_idempotent(self, tmp_path):
        data_dir = tmp_path / "data"
        ensure_git_repo(data_dir)
        ensure_git_repo(data_dir)  # does not raise or re-init


class TestSnapshotIfChanged:
    def test_first_snapshot_commits_when_data_present(self, tmp_path):
        data_dir = tmp_path / "data"
        _write_person_file(data_dir, "homer-simpson.md", "---\nid: homer-simpson\nname: Homer\n---\n")

        committed = snapshot_if_changed(data_dir)

        assert committed is True
        assert len(_git_log(data_dir)) == 1

    def test_no_changes_produces_no_commit(self, tmp_path):
        data_dir = tmp_path / "data"
        _write_person_file(data_dir, "homer-simpson.md", "---\nid: homer-simpson\nname: Homer\n---\n")
        snapshot_if_changed(data_dir)

        committed_again = snapshot_if_changed(data_dir)

        assert committed_again is False
        assert len(_git_log(data_dir)) == 1

    def test_second_change_produces_second_commit(self, tmp_path):
        data_dir = tmp_path / "data"
        _write_person_file(data_dir, "homer-simpson.md", "---\nid: homer-simpson\nname: Homer\n---\n")
        snapshot_if_changed(data_dir)

        _write_person_file(data_dir, "homer-simpson.md", "---\nid: homer-simpson\nname: Homer J.\n---\n")
        committed = snapshot_if_changed(data_dir)

        assert committed is True
        assert len(_git_log(data_dir)) == 2

    def test_no_data_at_all_produces_no_commit(self, tmp_path):
        data_dir = tmp_path / "data"
        committed = snapshot_if_changed(data_dir)
        assert committed is False

    def test_derived_index_and_secrets_are_never_committed(self, tmp_path):
        data_dir = tmp_path / "data"
        _write_person_file(data_dir, "homer-simpson.md", "---\nid: homer-simpson\nname: Homer\n---\n")
        (data_dir / "index").mkdir(parents=True, exist_ok=True)
        (data_dir / "index" / "relationships.sqlite").write_bytes(b"not a real sqlite file")
        (data_dir / "auth").mkdir(parents=True, exist_ok=True)
        (data_dir / "auth" / "credentials.sqlite").write_bytes(b"super secret token hashes")
        (data_dir / "validation-report.json").write_text("{}")

        snapshot_if_changed(data_dir)

        tracked = subprocess.run(
            ["git", "ls-files"], cwd=data_dir, capture_output=True, text=True, check=True
        ).stdout
        assert "index/" not in tracked
        assert "relationships.sqlite" not in tracked
        assert "auth/" not in tracked
        assert "credentials.sqlite" not in tracked
        assert "validation-report.json" not in tracked

    def test_archive_directory_is_also_captured(self, tmp_path):
        data_dir = tmp_path / "data"
        (data_dir / "archive").mkdir(parents=True, exist_ok=True)
        (data_dir / "archive" / "frank-grimes.md").write_text("---\nid: frank-grimes\nname: Frank\n---\n")

        snapshot_if_changed(data_dir)

        tracked = subprocess.run(
            ["git", "ls-files"], cwd=data_dir, capture_output=True, text=True, check=True
        ).stdout
        assert "archive/frank-grimes.md" in tracked

    def test_never_pushes_or_configures_a_remote(self, tmp_path):
        data_dir = tmp_path / "data"
        _write_person_file(data_dir, "homer-simpson.md", "---\nid: homer-simpson\nname: Homer\n---\n")
        snapshot_if_changed(data_dir)

        remotes = subprocess.run(
            ["git", "remote"], cwd=data_dir, capture_output=True, text=True, check=True
        ).stdout.strip()
        assert remotes == ""

    def test_commit_message_includes_todays_date_by_default(self, tmp_path):
        data_dir = tmp_path / "data"
        _write_person_file(data_dir, "homer-simpson.md", "---\nid: homer-simpson\nname: Homer\n---\n")
        snapshot_if_changed(data_dir)
        assert dt.date.today().isoformat() in _git_log(data_dir)[0]


class TestDailySnapshotScheduler:
    def test_seconds_until_next_run_later_today(self):
        scheduler = DailySnapshotScheduler("unused", daily_time=dt.time(15, 0))
        now = dt.datetime(2026, 8, 30, 10, 0, 0)
        assert scheduler._seconds_until_next_run(now) == 5 * 3600

    def test_seconds_until_next_run_wraps_to_tomorrow_when_time_has_passed(self):
        scheduler = DailySnapshotScheduler("unused", daily_time=dt.time(3, 0))
        now = dt.datetime(2026, 8, 30, 10, 0, 0)
        expected = (dt.datetime(2026, 8, 31, 3, 0, 0) - now).total_seconds()
        assert scheduler._seconds_until_next_run(now) == expected

    def test_seconds_until_next_run_exact_match_wraps_to_tomorrow(self):
        scheduler = DailySnapshotScheduler("unused", daily_time=dt.time(3, 0))
        now = dt.datetime(2026, 8, 30, 3, 0, 0)
        assert scheduler._seconds_until_next_run(now) == 24 * 3600

    @pytest.mark.asyncio
    async def test_start_stop_lifecycle_triggers_a_snapshot_at_the_scheduled_time(self, tmp_path):
        data_dir = tmp_path / "data"
        _write_person_file(data_dir, "homer-simpson.md", "---\nid: homer-simpson\nname: Homer\n---\n")

        soon = (dt.datetime.now() + dt.timedelta(milliseconds=50)).time()
        scheduler = DailySnapshotScheduler(data_dir, daily_time=soon)
        scheduler.start()
        try:
            import asyncio

            await asyncio.sleep(0.3)
            assert len(_git_log(data_dir)) == 1
        finally:
            await scheduler.stop()

    @pytest.mark.asyncio
    async def test_stop_before_scheduled_time_prevents_snapshot(self, tmp_path):
        data_dir = tmp_path / "data"
        _write_person_file(data_dir, "homer-simpson.md", "---\nid: homer-simpson\nname: Homer\n---\n")

        far_off = (dt.datetime.now() + dt.timedelta(hours=1)).time()
        scheduler = DailySnapshotScheduler(data_dir, daily_time=far_off)
        scheduler.start()
        await scheduler.stop()

        assert not (data_dir / ".git").exists()
