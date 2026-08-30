"""Startup repository scan and validation reporting (spec sections 18, 27, 28).

A malformed or inconsistent file must never prevent the server from
starting or block healthy records from being served (spec section 27):
every problem found here is recorded as a ValidationIssue and the scan
continues. Only "error"-severity issues exclude a person from the
returned valid set and mark the repository degraded; "warning"-severity
issues (a stale one-way relationship reference, a filename that no
longer matches its front-matter id) are recorded but never block
service, consistent with the spec's tolerant, non-reconciled,
one-way-link relationship model (section 15).
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from .markdown_store import MarkdownFormatError, MarkdownStore, markdown_to_person
from .models import Person

Severity = Literal["error", "warning"]

VALIDATION_REPORT_FILENAME = "validation-report.json"


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    file_path: str
    error_type: str
    severity: Severity
    message: str
    person_id: str | None = None


@dataclass(frozen=True, slots=True)
class ScanResult:
    valid: dict[str, Person]
    issues: list[ValidationIssue] = field(default_factory=list)
    scanned_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))

    @property
    def healthy(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]


def scan_people_directory(store: MarkdownStore) -> ScanResult:
    """Scan and validate every active person file in people/.

    Order of checks per file: parse (format/domain errors are fatal for
    that file only) -> filename/id consistency (warning) -> duplicate
    front-matter id across files (fatal for the later file). A final
    pass over the successfully-registered people flags dangling
    `related_person_id` references (warning).
    """
    issues: list[ValidationIssue] = []
    valid: dict[str, Person] = {}
    id_first_seen_at: dict[str, str] = {}

    for filename_stem in store.list_person_ids():
        path = store.person_path(filename_stem)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            issues.append(
                ValidationIssue(
                    file_path=str(path), error_type="io_error", severity="error", message=str(exc)
                )
            )
            continue

        try:
            person = markdown_to_person(text, source_path=str(path))
        except MarkdownFormatError as exc:
            issues.append(
                ValidationIssue(
                    file_path=str(path), error_type="format_error", severity="error", message=str(exc)
                )
            )
            continue
        except ValidationError as exc:
            issues.append(
                ValidationIssue(
                    file_path=str(path),
                    error_type="validation_error",
                    severity="error",
                    message=str(exc),
                )
            )
            continue

        if person.id != filename_stem:
            issues.append(
                ValidationIssue(
                    person_id=person.id,
                    file_path=str(path),
                    error_type="filename_id_mismatch",
                    severity="warning",
                    message=(
                        f"front-matter id {person.id!r} does not match filename "
                        f"'{filename_stem}.md'; the file should be renamed to match"
                    ),
                )
            )

        if person.id in id_first_seen_at:
            issues.append(
                ValidationIssue(
                    person_id=person.id,
                    file_path=str(path),
                    error_type="duplicate_person_id",
                    severity="error",
                    message=(
                        f"duplicate active person id {person.id!r}; already registered "
                        f"from {id_first_seen_at[person.id]!r}"
                    ),
                )
            )
            continue

        id_first_seen_at[person.id] = str(path)
        valid[person.id] = person

    for person in valid.values():
        for fact in person.facts:
            if fact.related_person_id and fact.related_person_id not in valid:
                issues.append(
                    ValidationIssue(
                        person_id=person.id,
                        file_path=str(store.person_path(person.id)),
                        error_type="dangling_related_person_id",
                        severity="warning",
                        message=(
                            f"fact {fact.id!r} on person {person.id!r} references "
                            f"related_person_id {fact.related_person_id!r}, which is not "
                            f"a known active person (may be archived or removed)"
                        ),
                    )
                )

    return ScanResult(valid=valid, issues=issues)


def validation_report_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / VALIDATION_REPORT_FILENAME


def write_validation_report(result: ScanResult, data_dir: Path | str) -> Path:
    """Write a fresh validation-report.json for `result` (spec section 28).
    Called on every startup and by the validate_repository MCP tool."""
    report = {
        "scanned_at": result.scanned_at.isoformat(),
        "healthy": result.healthy,
        "valid_person_count": len(result.valid),
        "error_count": len(result.errors),
        "warning_count": len(result.warnings),
        "issues": [
            {
                "person_id": issue.person_id,
                "file_path": issue.file_path,
                "error_type": issue.error_type,
                "severity": issue.severity,
                "message": issue.message,
            }
            for issue in result.issues
        ],
    }
    path = validation_report_path(data_dir)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return path
