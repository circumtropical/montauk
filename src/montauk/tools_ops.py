"""Birthday/cadence queries, archive tools, and validation/health
diagnostics (spec sections 16, 21, 26, 27, 28).
"""

from __future__ import annotations

import datetime as dt
import json

from mcp.server.mcpserver import MCPServer

from .dates import Birthday
from .errors import ArchivedError, MontaukValidationError, NotFoundError
from .markdown_store import MarkdownFormatError, PersonNotFoundError, person_to_markdown
from .reconciliation import scan_people_directory, validation_report_path, write_validation_report
from .tool_types import (
    ArchivedPersonSummary,
    ArchiveResult,
    HealthStatus,
    OverdueContact,
    UpcomingBirthday,
    ValidationIssueOut,
    ValidationReportSummary,
)
from .tools_core import MontaukContext


def _today(as_of: str | None) -> dt.date:
    if as_of is None:
        return dt.date.today()
    try:
        return dt.date.fromisoformat(as_of)
    except ValueError as exc:
        raise MontaukValidationError(f"as_of {as_of!r} is not a valid YYYY-MM-DD date") from exc


def _ensure_report(ctx: MontaukContext) -> dict:
    """Read the persisted validation report, generating a fresh one via a
    scan if none exists yet (e.g. before the server's own startup scan
    has run once). validate_repository() always forces a fresh scan;
    this is the shared self-healing read path for the other diagnostics
    tools."""
    path = validation_report_path(ctx.store.data_dir)
    if not path.exists():
        result = scan_people_directory(ctx.store)
        write_validation_report(result, ctx.store.data_dir)
    return json.loads(path.read_text(encoding="utf-8"))


def register_ops_tools(server: MCPServer, ctx: MontaukContext) -> None:
    store, sqlite_index, write_queue = ctx.store, ctx.sqlite_index, ctx.write_queue

    @server.tool(
        description=(
            "Return active people with a birthday in the next `within_days` days (default 30), "
            "soonest first. A birthday with no known year is still included -- age isn't computed. "
            "Deterministic: does not send notifications, just reports dates."
        )
    )
    async def get_upcoming_birthdays(within_days: int = 30, as_of: str | None = None) -> list[UpcomingBirthday]:
        today = _today(as_of)
        if within_days < 0:
            raise MontaukValidationError("within_days must be >= 0")
        upcoming: list[UpcomingBirthday] = []
        for row in sqlite_index.list_all():
            if row["birthday_month"] is None:
                continue
            birthday = Birthday(month=row["birthday_month"], day=row["birthday_day"], year=row["birthday_year"])
            occurrence = birthday.next_occurrence(today)
            days_until = (occurrence - today).days
            if days_until <= within_days:
                upcoming.append(
                    UpcomingBirthday(
                        person_id=row["person_id"],
                        name=row["name"],
                        birthday=birthday.to_string(),
                        next_occurrence=occurrence.isoformat(),
                        days_until=days_until,
                    )
                )
        upcoming.sort(key=lambda u: u.days_until)
        return upcoming

    @server.tool(
        description=(
            "Return active people who are overdue for contact relative to their desired_contact_"
            "cadence_days. A person with a cadence set but no recorded interaction is returned with "
            "status 'never_contacted' rather than being silently skipped or assigned a made-up date. "
            "People with no cadence set are never returned (no proactive keep-in-touch priority)."
        )
    )
    async def list_overdue_contacts(as_of: str | None = None) -> list[OverdueContact]:
        today = _today(as_of)
        overdue: list[OverdueContact] = []
        for row in sqlite_index.list_all():
            cadence = row["desired_contact_cadence_days"]
            if cadence is None:
                continue
            last_at = row["last_interaction_at"]
            if last_at is None:
                overdue.append(
                    OverdueContact(
                        person_id=row["person_id"],
                        name=row["name"],
                        desired_contact_cadence_days=cadence,
                        last_interaction_at=None,
                        days_since_last_interaction=None,
                        status="never_contacted",
                    )
                )
                continue
            days_since = (today - dt.date.fromisoformat(last_at)).days
            if days_since >= cadence:
                overdue.append(
                    OverdueContact(
                        person_id=row["person_id"],
                        name=row["name"],
                        desired_contact_cadence_days=cadence,
                        last_interaction_at=last_at,
                        days_since_last_interaction=days_since,
                        status="overdue",
                    )
                )
        overdue.sort(key=lambda o: (o.status != "never_contacted", -(o.days_since_last_interaction or 10**9)))
        return overdue

    @server.tool(
        description=(
            "Archive a known person_id: moves their record out of active search/birthday/cadence "
            "results into archive/, without deleting it. Reversible (an admin can move the file back "
            "and reconcile) -- this is not privacy erasure, and Git history still retains the record."
        )
    )
    async def archive_person(person_id: str) -> ArchiveResult:
        def op() -> ArchiveResult:
            if not store.exists(person_id):
                if store.is_archived(person_id):
                    raise ArchivedError(f"person {person_id!r} is already archived")
                raise NotFoundError(f"person {person_id!r} not found")
            store.archive_person(person_id)
            sqlite_index.remove_person(person_id)
            return ArchiveResult(person_id=person_id, status="archived")

        return await write_queue.submit(op)

    @server.tool(
        description=(
            "List archived people (id and name only). Archived people do not appear in search, "
            "birthday, or cadence results; use get_archived_person for their full record."
        )
    )
    async def list_archived_people() -> list[ArchivedPersonSummary]:
        summaries: list[ArchivedPersonSummary] = []
        for archived_id in store.list_archived_person_ids():
            try:
                person = store.read_archived_person(archived_id)
            except (MarkdownFormatError, PersonNotFoundError):
                continue
            summaries.append(ArchivedPersonSummary(person_id=person.id, name=person.name))
        return summaries

    @server.tool(
        description=(
            "Return the complete canonical Markdown record for a known archived person_id. Use "
            "list_archived_people first if you don't already have the person_id."
        )
    )
    async def get_archived_person(person_id: str) -> str:
        if store.exists(person_id):
            raise MontaukValidationError(f"person {person_id!r} is active, not archived; use get_full_record")
        try:
            person = store.read_archived_person(person_id)
        except PersonNotFoundError:
            raise NotFoundError(f"archived person {person_id!r} not found") from None
        return person_to_markdown(person)

    @server.tool(
        description=(
            "Force a fresh validation scan of the entire repository and return a summary (healthy, "
            "valid person count, error/warning counts, and the issues themselves). Read-only with "
            "respect to person data; refreshes data/validation-report.json as a side effect."
        )
    )
    async def validate_repository() -> ValidationReportSummary:
        result = scan_people_directory(store)
        write_validation_report(result, store.data_dir)
        return ValidationReportSummary(
            scanned_at=result.scanned_at.isoformat(),
            healthy=result.healthy,
            valid_person_count=len(result.valid),
            error_count=len(result.errors),
            warning_count=len(result.warnings),
            issues=[
                ValidationIssueOut(
                    person_id=i.person_id,
                    file_path=i.file_path,
                    error_type=i.error_type,
                    severity=i.severity,
                    message=i.message,
                )
                for i in result.issues
            ],
        )

    @server.tool(
        description=(
            "Return the issues from the most recent validation scan (without forcing a new one) -- "
            "use this to diagnose why an expected person is missing from search/index results. Call "
            "validate_repository first if you need the scan re-run right now."
        )
    )
    async def get_validation_errors() -> list[ValidationIssueOut]:
        report = _ensure_report(ctx)
        return [ValidationIssueOut(**issue) for issue in report["issues"]]

    @server.tool(
        description=(
            "Return operational health state (healthy/degraded, record counts, last scan/"
            "reconciliation times) without exposing any person content. Use get_validation_errors "
            "for the specific issues behind a degraded state."
        )
    )
    async def get_health_status() -> HealthStatus:
        report = _ensure_report(ctx)
        return HealthStatus(
            healthy=report["healthy"],
            valid_person_count=report["valid_person_count"],
            error_count=report["error_count"],
            warning_count=report["warning_count"],
            last_scanned_at=report["scanned_at"],
            last_reconciliation_at=sqlite_index.get_meta("last_reconciliation_at"),
        )
