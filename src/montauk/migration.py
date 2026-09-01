"""One-time cutover from name-derived person IDs (``mike-chen``) to generic
sequential IDs (``P0001``).

This is **not** part of normal runtime. ``montauk migrate-ids`` runs it once
per deployment. After it completes, only generic IDs are valid person
identifiers -- there is no legacy-ID resolver, alias, or lookup table.

The routine is idempotent: rerunning it after a completed migration is a
no-op, and rerunning after an interrupted migration converges using the
persisted old->new mapping without ever reassigning an ID.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

from .git_snapshot import snapshot_if_changed
from .ids import PERSON_ID_RE, format_person_id, person_id_number
from .markdown_store import MarkdownStore, atomic_write_text
from .reconciliation import scan_people_directory

MIGRATION_MAP_FILENAME = "id-migration-map.json"

_FRONT_MATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", re.DOTALL)
_FM_ID_LINE_RE = re.compile(r"^(id:\s*)(['\"]?)([^'\"\n]+)\2\s*$", re.MULTILINE)
_RELATED_LINE_RE = re.compile(r"^(\s*related_person_id:\s*)(['\"]?)([^'\"\n]+)\2(\s*)$", re.MULTILINE)


@dataclass
class MigrationReport:
    migrated: dict[str, str] = field(default_factory=dict)  # old_id -> new_id
    already_generic: list[str] = field(default_factory=list)
    references_rewritten: int = 0
    skipped: list[str] = field(default_factory=list)  # "path: reason"
    backup_ref: str | None = None
    verified: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.migrated)


def _iter_person_files(store: MarkdownStore) -> list[Path]:
    return sorted(store.people_dir.glob("*.md")) + sorted(store.archive_dir.glob("*.md"))


def _read_front_matter_id(path: Path) -> str | None:
    m = _FRONT_MATTER_RE.match(path.read_text(encoding="utf-8"))
    if not m:
        return None
    try:
        fm = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None
    if isinstance(fm, dict) and isinstance(fm.get("id"), str):
        return fm["id"].strip()
    return None


def _load_map(data_dir: Path) -> dict[str, str]:
    path = data_dir / MIGRATION_MAP_FILENAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items()}
    except (OSError, ValueError):
        return {}


def _save_map(data_dir: Path, mapping: dict[str, str]) -> None:
    atomic_write_text(data_dir / MIGRATION_MAP_FILENAME, json.dumps(mapping, indent=2, sort_keys=True) + "\n")


def _rewrite_file(path: Path, *, new_id: str | None, mapping: dict[str, str]) -> int:
    """Rewrite one person file's own id (if new_id given) and every
    related_person_id reference found in `mapping`. Returns the number of
    reference lines rewritten. Only these two fields are identity-bearing
    in the schema; everything else is left byte-for-byte intact."""
    text = path.read_text(encoding="utf-8")
    fm_match = _FRONT_MATTER_RE.match(text)
    refs = 0

    if fm_match and new_id is not None:
        fm_text, body = fm_match.group(1), fm_match.group(2)
        fm_text = _FM_ID_LINE_RE.sub(lambda m: f"{m.group(1)}{new_id}", fm_text, count=1)
        text = f"---\n{fm_text}\n---\n{body}"

    def _sub_related(m: re.Match[str]) -> str:
        nonlocal refs
        old = m.group(3).strip()
        if old in mapping:
            refs += 1
            return f"{m.group(1)}{mapping[old]}{m.group(4)}"
        return m.group(0)

    text = _RELATED_LINE_RE.sub(_sub_related, text)
    atomic_write_text(path, text)
    return refs


def migrate_person_ids(store: MarkdownStore, *, make_backup: bool = True) -> MigrationReport:
    data_dir = store.data_dir
    report = MigrationReport()

    files = _iter_person_files(store)
    current: list[tuple[Path, str]] = []
    for path in files:
        pid = _read_front_matter_id(path)
        if pid is None:
            report.skipped.append(f"{path}: could not read a front-matter id")
            continue
        current.append((path, pid))

    if report.skipped:
        raise MigrationError(
            "refusing to migrate: some person files could not be parsed for their id "
            "(fix or remove them first):\n  " + "\n  ".join(report.skipped)
        )

    generic = {pid for _p, pid in current if PERSON_ID_RE.match(pid)}
    legacy = [(p, pid) for p, pid in current if not PERSON_ID_RE.match(pid)]
    report.already_generic = sorted(generic)

    if not legacy:
        # Nothing to do; still make sure the allocator can't collide.
        store.sync_id_sequence()
        report.verified = _verify(store)
        return report

    # Deterministic assignment: resume from a persisted map, then assign
    # remaining legacy ids in canonical order (active files first, then
    # archive, each lexically by their current id).
    mapping = dict(_load_map(data_dir))
    next_n = max(
        [person_id_number(v) or 0 for v in mapping.values()]
        + [person_id_number(pid) or 0 for pid in generic]
        + [0]
    )
    active = sorted((pid for p, pid in legacy if p.parent == store.people_dir))
    archived = sorted((pid for p, pid in legacy if p.parent == store.archive_dir))
    for old_id in [*active, *archived]:
        if old_id not in mapping:
            next_n += 1
            mapping[old_id] = format_person_id(next_n)

    if make_backup:
        report.backup_ref = _make_backup(data_dir)

    _save_map(data_dir, mapping)

    # 1. Rewrite every file's own id + all references, in place.
    for path, pid in current:
        new_id = mapping.get(pid)
        report.references_rewritten += _rewrite_file(path, new_id=new_id, mapping=mapping)

    # 2. Rename files whose stem no longer matches their (new) id.
    for path, pid in current:
        new_id = mapping.get(pid)
        if new_id is None:
            continue
        target = path.with_name(f"{new_id}.md")
        if path != target:
            path.rename(target)
        report.migrated[pid] = new_id

    # 3. Advance the permanent allocator past every id now on disk.
    store.id_sequence.initialize()
    store.sync_id_sequence()

    report.verified = _verify(store)
    if not report.verified:
        raise MigrationError(
            "migration completed writes but post-migration verification failed; "
            f"a backup is at {report.backup_ref!r}"
        )
    return report


def _verify(store: MarkdownStore) -> bool:
    """Every person file has a canonical generic id, every filename matches
    its id, and no related_person_id dangles."""
    scan = scan_people_directory(store)
    if not scan.healthy:
        return False
    stems = {*store.list_person_ids(), *store.list_archived_person_ids()}
    if any(not PERSON_ID_RE.match(s) for s in stems):
        return False
    return all(PERSON_ID_RE.match(person_id) for person_id in scan.valid)


def _make_backup(data_dir: Path) -> str:
    """Prefer a git snapshot (the project's established recovery mechanism);
    fall back to a copied directory when git is unavailable."""
    try:
        if snapshot_if_changed(data_dir, message="Pre-migration snapshot (name-derived -> generic IDs)"):
            return "git:pre-migration-snapshot"
        return "git:no-change (working tree already clean)"
    except Exception:  # noqa: BLE001 -- fall back to a plain copy
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup_dir = data_dir / f".montauk-migration-backup-{stamp}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        for sub in ("people", "archive"):
            if (data_dir / sub).exists():
                shutil.copytree(data_dir / sub, backup_dir / sub)
        return str(backup_dir)


class MigrationError(Exception):
    pass
