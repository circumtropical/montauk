"""Daily Git snapshot of canonical Markdown data (spec section 29).

Git here is an audit/recovery mechanism, not part of the logical
database schema, and is entirely independent of whatever source-control
repository Montauk's own code lives in: this module `git init`s (once)
and commits *inside* the deployment's data_dir itself, since a real
deployment's data_dir is typically outside the source tree entirely
(e.g. /var/lib/montauk). At most one automatic commit per day, only if
canonical data actually changed since the last commit; never
auto-pushes; never commits derived indexes (index/) or credentials
(auth/) -- both are excluded by a .gitignore this module creates
alongside the repo, and by only ever `git add`-ing the canonical paths
(people/, archive/, and the person-id sequence file) explicitly (never
`-A`/`.`).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import subprocess
from collections.abc import Sequence
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_DAILY_TIME = dt.time(3, 0)
DEFAULT_BRANCH = "main"

# Canonical, git-tracked paths (everything else under data_dir is derived
# or secret). people/ and archive/ hold the Markdown records;
# person-id-sequence.json is the permanent ID high-water mark.
CANONICAL_PATHS = ("people", "archive", "person-id-sequence.json")

_DATA_GITIGNORE = (
    "# Derived/disposable indexes and credentials are never snapshotted.\n"
    "index/\n"
    "auth/\n"
    "logs/\n"
    "validation-report.json\n"
    ".montauk.lock\n"
    "id-migration-map.json\n"
    ".montauk-migration-backup-*/\n"
)

# Automated commits (initial scaffold + daily snapshots) get a clearly
# bot-like identity distinct from any human owner, scoped to just the
# invocation so no persistent git config write is needed and it works
# regardless of the environment's global git config.
_COMMIT_IDENTITY = ["-c", "user.name=Montauk", "-c", "user.email=montauk@localhost"]


class GitSnapshotError(Exception):
    pass


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def ensure_git_repo(data_dir: Path | str, *, default_branch: str = DEFAULT_BRANCH) -> None:
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    if (data_dir / ".git").exists():
        return
    result = _run_git(["init"], cwd=data_dir)
    if result.returncode != 0:
        raise GitSnapshotError(f"git init failed in {data_dir}: {result.stderr}")
    # Pin the initial branch name deterministically instead of depending on
    # the host git's version or init.defaultBranch. Safe here: the repo was
    # just created, the branch is unborn, and there are no refs yet.
    branch_result = _run_git(["symbolic-ref", "HEAD", f"refs/heads/{default_branch}"], cwd=data_dir)
    if branch_result.returncode != 0:
        raise GitSnapshotError(f"git symbolic-ref failed in {data_dir}: {branch_result.stderr}")
    gitignore = data_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(_DATA_GITIGNORE, encoding="utf-8")


def repo_has_commits(data_dir: Path | str) -> bool:
    result = _run_git(["rev-parse", "--verify", "--quiet", "HEAD"], cwd=Path(data_dir))
    return result.returncode == 0


def create_initial_commit(
    data_dir: Path | str, *, message: str, extra_paths: Sequence[str] = ()
) -> bool:
    """Make the first commit in a freshly-initialised data repo so the branch
    exists and can be pushed to a remote. No-op (returns False) if the repo
    already has any commit, or if there is nothing to commit. Montauk never
    adds a remote or pushes -- that is left to the operator (spec section 29).
    """
    data_dir = Path(data_dir)
    ensure_git_repo(data_dir)
    if repo_has_commits(data_dir):
        return False
    paths = [p for p in (".gitignore", *extra_paths) if (data_dir / p).exists()]
    if not paths:
        return False
    add_result = _run_git(["add", "--", *paths], cwd=data_dir)
    if add_result.returncode != 0:
        raise GitSnapshotError(f"git add failed in {data_dir}: {add_result.stderr}")
    commit_result = _run_git([*_COMMIT_IDENTITY, "commit", "-m", message], cwd=data_dir)
    if commit_result.returncode != 0:
        raise GitSnapshotError(f"git commit failed in {data_dir}: {commit_result.stderr}")
    return True


def snapshot_if_changed(data_dir: Path | str, *, message: str | None = None) -> bool:
    """Commit people/ and archive/ if they changed since the last commit
    in the data_dir-local repo. Returns True if a commit was created,
    False if there was nothing to commit. Idempotent: safe to call
    repeatedly (e.g. both the in-process daily timer and an external
    cron entry landing on the same day) -- at most one commit results.
    """
    data_dir = Path(data_dir)
    ensure_git_repo(data_dir)

    # Explicit pathspecs (unlike `git add -A`/`.`) error out if a given
    # path doesn't exist at all, which is expected for a brand-new
    # deployment before people/ or archive/ has ever been created.
    data_pathspecs = [p for p in CANONICAL_PATHS if (data_dir / p).exists()]
    if not data_pathspecs:
        return False

    # Check status scoped to the canonical data dirs *before* staging
    # anything, so a freshly-created .gitignore with no real data yet
    # doesn't itself count as a snapshot-worthy change.
    status_result = _run_git(["status", "--porcelain", "--", *data_pathspecs], cwd=data_dir)
    if status_result.returncode != 0:
        raise GitSnapshotError(f"git status failed in {data_dir}: {status_result.stderr}")
    if not status_result.stdout.strip():
        return False

    add_result = _run_git(["add", "--", *data_pathspecs, ".gitignore"], cwd=data_dir)
    if add_result.returncode != 0:
        raise GitSnapshotError(f"git add failed in {data_dir}: {add_result.stderr}")

    commit_message = message or f"Montauk daily snapshot {dt.date.today().isoformat()}"
    commit_result = _run_git([*_COMMIT_IDENTITY, "commit", "-m", commit_message], cwd=data_dir)
    if commit_result.returncode != 0:
        raise GitSnapshotError(f"git commit failed in {data_dir}: {commit_result.stderr}")
    return True


class DailySnapshotScheduler:
    """Runs snapshot_if_changed once daily at a configured local time, as
    a background asyncio task inside the long-running server process.
    Not used by one-shot CLI invocations (`montauk git-snapshot` calls
    snapshot_if_changed directly).
    """

    def __init__(self, data_dir: Path | str, *, daily_time: dt.time = DEFAULT_DAILY_TIME):
        self.data_dir = Path(data_dir)
        self.daily_time = daily_time
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    def _seconds_until_next_run(self, now: dt.datetime | None = None) -> float:
        now = now or dt.datetime.now()
        target = dt.datetime.combine(now.date(), self.daily_time)
        if target <= now:
            target += dt.timedelta(days=1)
        return (target - now).total_seconds()

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            delay = self._seconds_until_next_run()
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                break  # stop() was called
            except TimeoutError:
                pass
            try:
                snapshot_if_changed(self.data_dir)
            except GitSnapshotError:
                logger.exception("daily git snapshot failed for %s", self.data_dir)

    def start(self) -> None:
        if self._task is None:
            self._stop_event.clear()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            await self._task
            self._task = None
