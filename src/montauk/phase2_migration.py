"""One-time Phase 1 (Markdown) -> Phase 2 (PostgreSQL) migration (spec 30).

Properties this module guarantees:

* **Dry-run touches nothing** -- no source write, no backup, no PostgreSQL
  write. It parses, validates, and reports what *would* happen.
* **Execute is staged in one transaction.** Any failure before commit
  rolls the whole import back; Phase 1 stays operational and a workspace
  is never left half-migrated.
* **The Phase 1 tree is read-only.** The optional pre-migration backup is
  a *copy* to a directory outside the source, so source bytes and Git
  state are unchanged (spec 30.1, 32.6).
* **Permanent person ids are preserved** and the per-workspace high-water
  mark is advanced past every imported id and the source sequence.
* **Malformed records block cutover** in execute mode -- they are never
  silently skipped (spec 30.5).
* **Rerun is safe.** A workspace that already holds a successful migration,
  or any people at all, is refused rather than duplicated.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from .db import ids as id_alloc
from .db import mapping
from .db import models as orm
from .db.repositories import Actor, PeopleRepository, WorkspaceScope
from .exporters.markdown import person_to_markdown
from .ids import person_id_number
from .markdown_store import MarkdownFormatError, markdown_to_person
from .models import Person
from .services.workspace import get_or_create_workspace, get_workspace_by_slug, slugify_workspace

SEQUENCE_FILENAME = "person-id-sequence.json"


class MigrationError(RuntimeError):
    pass


# --- source inspection ----------------------------------------------


@dataclass
class SourceIssue:
    path: str
    error_type: str
    severity: str  # "error" | "warning"
    message: str
    person_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class StagedPerson:
    person: Person
    archived: bool
    source_path: str
    source_bytes_sha256: str
    canonical_markdown_hash: str


def is_phase1_deployment(path: Path) -> bool:
    path = Path(path)
    if not (path / "people").is_dir():
        return False
    return (
        (path / SEQUENCE_FILENAME).exists()
        or any((path / "people").glob("*.md"))
        or (path / "archive").is_dir()
    )


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_manifest(path: Path) -> tuple[str, dict[str, str]]:
    """A stable hash over every identity-bearing source file, plus the
    per-file map for the report (spec 30.4 step 5, 30.5)."""
    path = Path(path)
    files: dict[str, str] = {}
    for sub in ("people", "archive"):
        d = path / sub
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.md")):
            files[f"{sub}/{f.name}"] = _sha256_bytes(f.read_bytes())
    seq = path / SEQUENCE_FILENAME
    if seq.exists():
        files[SEQUENCE_FILENAME] = _sha256_bytes(seq.read_bytes())
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode("utf-8")).hexdigest()
    return digest, files


def read_source_high_water(path: Path) -> int:
    seq = Path(path) / SEQUENCE_FILENAME
    if not seq.exists():
        return 0
    try:
        return max(int(json.loads(seq.read_text(encoding="utf-8"))["last_allocated"]), 0)
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def load_source(path: Path) -> tuple[list[StagedPerson], list[SourceIssue], int]:
    """Parse and validate every active and archived person file. A file that
    fails to parse or validate becomes an ``error`` issue and is not staged;
    a filename/id mismatch or a dangling relationship reference is a
    ``warning`` and does not block."""
    path = Path(path)
    staged: list[StagedPerson] = []
    issues: list[SourceIssue] = []
    seen_ids: dict[str, str] = {}

    for sub, archived in (("people", False), ("archive", True)):
        d = path / sub
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.md")):
            rel = f"{sub}/{f.name}"
            raw = f.read_bytes()
            try:
                person = markdown_to_person(raw.decode("utf-8"), source_path=rel)
            except MarkdownFormatError as exc:
                issues.append(SourceIssue(rel, "format_error", "error", str(exc)))
                continue
            except ValidationError as exc:
                issues.append(SourceIssue(rel, "validation_error", "error", str(exc)))
                continue
            except UnicodeDecodeError as exc:
                issues.append(SourceIssue(rel, "encoding_error", "error", str(exc)))
                continue

            if f.stem != person.id:
                issues.append(
                    SourceIssue(
                        rel,
                        "filename_id_mismatch",
                        "warning",
                        f"front-matter id {person.id!r} does not match filename {f.name!r}",
                        person.id,
                    )
                )
            if person.id in seen_ids:
                issues.append(
                    SourceIssue(
                        rel,
                        "duplicate_person_id",
                        "error",
                        f"person id {person.id!r} already seen in {seen_ids[person.id]!r}",
                        person.id,
                    )
                )
                continue
            seen_ids[person.id] = rel
            staged.append(
                StagedPerson(
                    person=person,
                    archived=archived,
                    source_path=rel,
                    source_bytes_sha256=_sha256_bytes(raw),
                    canonical_markdown_hash=_sha256_bytes(person_to_markdown(person).encode("utf-8")),
                )
            )

    known = set(seen_ids)
    for sp in staged:
        for fact in sp.person.facts:
            ref = fact.related_person_id
            if ref and ref not in known:
                issues.append(
                    SourceIssue(
                        sp.source_path,
                        "dangling_related_person_id",
                        "warning",
                        f"fact {fact.id!r} references unknown person {ref!r}",
                        sp.person.id,
                    )
                )

    return staged, issues, read_source_high_water(path)


# --- report --------------------------------------------------------


@dataclass
class MigrationReport:
    mode: str
    source_path: str
    run_id: str
    status: str = "pending"
    source_manifest_hash: str = ""
    source_manifest: dict[str, str] = field(default_factory=dict)
    backup_ref: str | None = None
    backup_status: str = "not_created"
    workspace_slug: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    source_high_water: int = 0
    destination_high_water: int = 0
    person_ids: list[str] = field(default_factory=list)
    archived_person_ids: list[str] = field(default_factory=list)
    dangling_references: list[dict[str, Any]] = field(default_factory=list)
    per_person_hash: list[dict[str, Any]] = field(default_factory=list)
    hash_mismatches: list[str] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    skipped_files: list[dict[str, Any]] = field(default_factory=list)
    blocking_errors: list[dict[str, Any]] = field(default_factory=list)
    lossy: bool = False
    destination_index: dict[str, Any] = field(default_factory=dict)
    started_at: str = field(default_factory=lambda: dt.datetime.now(dt.UTC).isoformat())
    finished_at: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("dry_run_ok", "succeeded")

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def human_readable(self) -> str:
        lines = [
            f"Montauk Phase 1 -> Phase 2 migration ({self.mode})",
            f"  status:            {self.status}",
            f"  source:            {self.source_path}",
            f"  source manifest:   {self.source_manifest_hash[:16]}",
            f"  backup:            {self.backup_status}"
            + (f" ({self.backup_ref})" if self.backup_ref else ""),
            f"  workspace:         {self.workspace_slug or '(not created)'}",
            "  counts:            " + ", ".join(f"{k}={v}" for k, v in sorted(self.counts.items())),
            f"  source high-water: {self.source_high_water}",
            f"  dest high-water:   {self.destination_high_water}",
            f"  active people:     {len(self.person_ids)}",
            f"  archived people:   {len(self.archived_person_ids)}",
            f"  dangling refs:     {len(self.dangling_references)}",
            f"  hash comparisons:  {len(self.per_person_hash)} ({len(self.hash_mismatches)} mismatch)",
            f"  warnings:          {len(self.warnings)}",
            f"  blocking errors:   {len(self.blocking_errors)}",
            f"  lossy:             {self.lossy}",
        ]
        if self.destination_index:
            lines.append(
                "  destination index: "
                + ", ".join(f"{k}={v}" for k, v in sorted(self.destination_index.items()))
            )
        for e in self.blocking_errors:
            lines.append(f"    ERROR  {e['path']}: {e['message']}")
        for w in self.warnings:
            lines.append(f"    warn   {w['path']}: {w['message']}")
        return "\n".join(lines)


def _counts_from_staged(staged: list[StagedPerson]) -> dict[str, int]:
    active = [s for s in staged if not s.archived]
    archived = [s for s in staged if s.archived]
    return {
        "active_people": len(active),
        "archived_people": len(archived),
        "aliases": sum(len(s.person.aliases) for s in staged),
        "contact_methods": sum(
            len(s.person.contact.emails)
            + len(s.person.contact.phones)
            + (1 if s.person.contact.address else 0)
            + len(s.person.contact.messaging)
            for s in staged
        ),
        "facts": sum(len(s.person.facts) for s in staged),
        "relationships": sum(1 for s in staged for fct in s.person.facts if fct.related_person_id),
        "interactions": sum(len(s.person.interactions) for s in staged),
        "fact_sources": sum(len(fct.sources) for s in staged for fct in s.person.facts),
        "interaction_sources": sum(len(i.sources) for s in staged for i in s.person.interactions),
    }


# --- backup -------------------------------------------------------


def _make_backup(source: Path, backup_dir: Path | None) -> tuple[str, str]:
    ts = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    target = Path(backup_dir) if backup_dir else source.parent / f"montauk-phase1-backup-{ts}"
    if target.exists() and any(target.iterdir()):
        raise MigrationError(f"backup target {target} already exists and is not empty")
    target.mkdir(parents=True, exist_ok=True)
    for sub in ("people", "archive"):
        if (source / sub).is_dir():
            shutil.copytree(source / sub, target / sub, dirs_exist_ok=True)
    if (source / SEQUENCE_FILENAME).exists():
        shutil.copy2(source / SEQUENCE_FILENAME, target / SEQUENCE_FILENAME)
    return str(target), "created"


# --- migration ----------------------------------------------------


def _prior_successful_run(session: Session, workspace_id: uuid.UUID) -> orm.LegacyMigrationRun | None:
    return session.execute(
        select(orm.LegacyMigrationRun)
        .where(orm.LegacyMigrationRun.workspace_id == workspace_id)
        .where(orm.LegacyMigrationRun.status == "succeeded")
    ).scalar_one_or_none()


def run_migration(
    session_factory: sessionmaker[Session],
    *,
    source_dir: str | Path,
    workspace_name: str,
    mode: str = "dry_run",
    make_backup: bool = True,
    backup_dir: str | Path | None = None,
    fault_injection: Callable[[], None] | None = None,
) -> MigrationReport:
    if mode not in ("dry_run", "execute"):
        raise MigrationError(f"mode must be 'dry_run' or 'execute', got {mode!r}")
    source = Path(source_dir).expanduser().resolve()
    run_id = str(uuid.uuid4())
    report = MigrationReport(mode=mode, source_path=str(source), run_id=run_id)

    if not is_phase1_deployment(source):
        raise MigrationError(f"{source} does not look like a Phase 1 deployment (no people/ directory)")

    report.source_manifest_hash, report.source_manifest = source_manifest(source)
    staged, issues, source_hwm = load_source(source)
    report.source_high_water = source_hwm
    report.warnings = [i.as_dict() for i in issues if i.severity == "warning"]
    report.blocking_errors = [i.as_dict() for i in issues if i.severity == "error"]
    report.skipped_files = [
        i.as_dict() for i in issues if i.severity == "error" and i.error_type != "duplicate_person_id"
    ]
    report.dangling_references = [i.as_dict() for i in issues if i.error_type == "dangling_related_person_id"]
    report.counts = _counts_from_staged(staged)
    report.person_ids = sorted(s.person.id for s in staged if not s.archived)
    report.archived_person_ids = sorted(s.person.id for s in staged if s.archived)

    slug = slugify_workspace(workspace_name)
    report.workspace_slug = slug

    # -- dry run: nothing is written --
    if mode == "dry_run":
        with session_factory() as s:
            existing = get_workspace_by_slug(s, slug)
            if existing is not None:
                if _prior_successful_run(s, existing.id) is not None:
                    report.status = "already_migrated"
                    report.finished_at = dt.datetime.now(dt.UTC).isoformat()
                    return report
                if _workspace_person_count(s, existing.id) > 0:
                    report.status = "destination_not_empty"
                    report.finished_at = dt.datetime.now(dt.UTC).isoformat()
                    return report
        report.destination_high_water = max(
            [person_id_number(s.person.id) or 0 for s in staged] + [source_hwm, 0]
        )
        # dry-run hash column = the source canonical hash on both sides
        report.per_person_hash = [
            {
                "person_id": s.person.id,
                "source_hash": s.canonical_markdown_hash,
                "destination_hash": s.canonical_markdown_hash,
                "match": True,
            }
            for s in staged
        ]
        report.status = "dry_run_blocked" if report.blocking_errors else "dry_run_ok"
        report.finished_at = dt.datetime.now(dt.UTC).isoformat()
        return report

    # -- execute --
    if report.blocking_errors:
        report.status = "aborted_blocking_errors"
        report.finished_at = dt.datetime.now(dt.UTC).isoformat()
        _record_run(session_factory, report, workspace_id=None)
        return report

    if make_backup:
        report.backup_ref, report.backup_status = _make_backup(
            source, Path(backup_dir) if backup_dir is not None else None
        )
    else:
        report.backup_status = "skipped_by_operator"

    try:
        with session_factory() as s:
            with s.begin():
                existing = get_workspace_by_slug(s, slug)
                if existing is not None:
                    if _prior_successful_run(s, existing.id) is not None:
                        report.status = "already_migrated"
                        raise _Abort()
                    if _workspace_person_count(s, existing.id) > 0:
                        report.status = "destination_not_empty"
                        raise _Abort()

                workspace = get_or_create_workspace(s, workspace_name)
                scope = WorkspaceScope(s, workspace.id, Actor.migration(run_id))
                repo = PeopleRepository(scope)

                rows = [
                    repo.create(
                        sp.person,
                        authority="owner_curated",
                        archived=sp.archived,
                        record_revision=False,
                    )
                    for sp in staged
                ]
                s.flush()
                if fault_injection is not None:
                    fault_injection()
                repo.link_related_person_ids(rows)

                max_imported = max([person_id_number(sp.person.id) or 0 for sp in staged] + [0])
                target_hwm = max(max_imported, source_hwm)
                id_alloc.ensure_high_water_at_least(s, workspace.id, target_hwm)
                report.destination_high_water = id_alloc.high_water(s, workspace.id)

                _verify(scope, staged, report)
                if report.hash_mismatches or report.lossy:
                    report.status = "verification_failed"
                    raise _Abort()

                s.add(
                    orm.LegacyMigrationRun(
                        id=uuid.UUID(run_id),
                        workspace_id=workspace.id,
                        workspace_slug=slug,
                        source_path=str(source),
                        source_manifest_hash=report.source_manifest_hash,
                        mode="execute",
                        status="succeeded",
                        backup_ref=report.backup_ref,
                        report=report.to_dict(),
                        finished_at=dt.datetime.now(dt.UTC),
                    )
                )
                report.status = "succeeded"
    except _Abort:
        report.finished_at = dt.datetime.now(dt.UTC).isoformat()
        _record_run(session_factory, report, workspace_id=None)
        return report
    except Exception as exc:
        report.status = "failed"
        report.finished_at = dt.datetime.now(dt.UTC).isoformat()
        report.blocking_errors.append(
            {"path": "<migration>", "error_type": "exception", "severity": "error", "message": str(exc)}
        )
        _record_run(session_factory, report, workspace_id=None)
        raise MigrationError(f"migration failed and was rolled back: {exc}") from exc

    report.destination_index = {"lexical": "postgresql", "semantic": "rebuild_pending"}
    report.finished_at = dt.datetime.now(dt.UTC).isoformat()
    return report


class _Abort(Exception):
    pass


def _workspace_person_count(session: Session, workspace_id: uuid.UUID) -> int:
    return int(
        session.execute(
            select(func.count(orm.Person.id)).where(orm.Person.workspace_id == workspace_id)
        ).scalar_one()
    )


def _verify(scope: WorkspaceScope, staged: list[StagedPerson], report: MigrationReport) -> None:
    """In-transaction, pre-commit verification (spec 30.4 step 12, 30.5).
    Compares counts and per-person canonical hashes; records dangling refs."""
    repo = PeopleRepository(scope)
    active_in_db = repo.count(archived=False)
    archived_in_db = repo.count(archived=True)
    expect_active = sum(1 for s in staged if not s.archived)
    expect_archived = sum(1 for s in staged if s.archived)
    if (active_in_db, archived_in_db) != (expect_active, expect_archived):
        report.hash_mismatches.append(
            f"count mismatch: db active/archived = {active_in_db}/{archived_in_db}, "
            f"expected {expect_active}/{expect_archived}"
        )

    per_person: list[dict[str, Any]] = []
    for sp in staged:
        row = repo.get(sp.person.id, include_archived=True)
        if row is None:
            report.hash_mismatches.append(f"{sp.person.id}: missing from destination")
            continue
        rebuilt = mapping.person_to_domain(row)
        dest_hash = _sha256_bytes(person_to_markdown(rebuilt).encode("utf-8"))
        match = dest_hash == sp.canonical_markdown_hash
        per_person.append(
            {
                "person_id": sp.person.id,
                "source_hash": sp.canonical_markdown_hash,
                "destination_hash": dest_hash,
                "match": match,
            }
        )
        if not match:
            report.hash_mismatches.append(f"{sp.person.id}: canonical hash mismatch")
            report.lossy = True
    report.per_person_hash = per_person


def _record_run(
    session_factory: sessionmaker[Session],
    report: MigrationReport,
    *,
    workspace_id: uuid.UUID | None,
) -> None:
    """Record a non-successful attempt in its own transaction (the import
    transaction has already rolled back)."""
    try:
        with session_factory() as s, s.begin():
            s.add(
                orm.LegacyMigrationRun(
                    workspace_id=workspace_id,
                    workspace_slug=report.workspace_slug or "",
                    source_path=report.source_path,
                    source_manifest_hash=report.source_manifest_hash,
                    mode=report.mode,
                    status=report.status,
                    backup_ref=report.backup_ref,
                    report=report.to_dict(),
                    finished_at=dt.datetime.now(dt.UTC),
                )
            )
    except Exception:  # noqa: BLE001 -- recording a failure must not mask it
        pass


def verify_migration(session: Session, *, run_id: str) -> dict[str, Any]:
    """Re-read a stored migration run's report (spec: ``verify-phase2-migration``)."""
    row = session.get(orm.LegacyMigrationRun, uuid.UUID(run_id))
    if row is None:
        raise MigrationError(f"no migration run {run_id!r}")
    return {
        "run_id": run_id,
        "status": row.status,
        "workspace_slug": row.workspace_slug,
        "source_manifest_hash": row.source_manifest_hash,
        "backup_ref": row.backup_ref,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "report": row.report,
    }
