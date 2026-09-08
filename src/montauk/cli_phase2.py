"""Phase 2 CLI: PostgreSQL schema management and the Phase 1 -> Phase 2
migration (spec 30.4). Mounted on the main ``montauk`` app in cli.py.
"""

from __future__ import annotations

import json as _json
from pathlib import Path

import typer

from .db.engine import DatabaseNotConfigured, create_db_engine, resolve_url, session_factory
from .db.schema_ops import current_revision, downgrade_to, head_revision, upgrade_to_head
from .phase2_migration import MigrationError, run_migration, verify_migration

db_app = typer.Typer(add_completion=False, no_args_is_help=True, help="PostgreSQL schema management.")
migrate_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Phase 1 -> Phase 2 migration.")

DbUrlOption = typer.Option(
    None, "--database-url", help="PostgreSQL DSN (else MONTAUK_DATABASE_URL / DATABASE_URL)."
)


def _require_url(explicit: str | None) -> str:
    try:
        return resolve_url(explicit)
    except DatabaseNotConfigured as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


@db_app.command("upgrade")
def db_upgrade(database_url: str | None = DbUrlOption) -> None:
    """Apply all pending migrations (creates the schema on an empty database)."""
    url = _require_url(database_url)
    upgrade_to_head(url)
    typer.echo(f"schema at {current_revision(url)} (head {head_revision(url)})")


@db_app.command("current")
def db_current(database_url: str | None = DbUrlOption) -> None:
    url = _require_url(database_url)
    cur, head = current_revision(url), head_revision(url)
    typer.echo(f"current: {cur}")
    typer.echo(f"head:    {head}")
    typer.echo("up to date" if cur == head else "MIGRATIONS PENDING -> run `montauk db upgrade`")


@db_app.command("downgrade")
def db_downgrade(
    revision: str = typer.Argument(..., help="Target revision, or 'base'."),
    database_url: str | None = DbUrlOption,
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt."),
) -> None:
    url = _require_url(database_url)
    if not yes:
        typer.confirm(f"Downgrade schema to {revision!r}? This can drop tables and data.", abort=True)
    downgrade_to(revision, url)
    typer.echo(f"schema at {current_revision(url)}")


def _run(
    *,
    source_data_dir: Path,
    workspace: str,
    execute: bool,
    backup: bool,
    backup_dir: Path | None,
    as_json: bool,
    database_url: str | None,
) -> None:
    url = _require_url(database_url)
    engine = create_db_engine(url)
    factory = session_factory(engine)
    try:
        report = run_migration(
            factory,
            source_dir=source_data_dir,
            workspace_name=workspace,
            mode="execute" if execute else "dry_run",
            make_backup=backup,
            backup_dir=backup_dir,
        )
    except MigrationError as exc:
        typer.echo(f"migration error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        engine.dispose()

    if as_json:
        typer.echo(_json.dumps(report.to_dict(), indent=2))
    else:
        typer.echo(report.human_readable())
        typer.echo("")
        typer.echo(f"run id: {report.run_id}")
    if not report.ok:
        raise typer.Exit(code=1)


@migrate_app.command("phase2")
def migrate_phase2(
    source_data_dir: Path = typer.Option(..., "--source-data-dir", help="Phase 1 data directory."),
    workspace: str = typer.Option(..., "--workspace", help="Target workspace name."),
    execute: bool = typer.Option(False, "--execute", help="Apply the migration (default: dry run)."),
    backup: bool = typer.Option(True, "--backup/--no-backup", help="Copy the Phase 1 tree first."),
    backup_dir: Path | None = typer.Option(None, "--backup-dir", help="Where to write the backup copy."),
    as_json: bool = typer.Option(False, "--json", help="Emit the machine-readable report."),
    database_url: str | None = DbUrlOption,
) -> None:
    """Import a Phase 1 Markdown deployment into PostgreSQL. Dry-run by
    default: nothing is written until you pass --execute."""
    _run(
        source_data_dir=source_data_dir,
        workspace=workspace,
        execute=execute,
        backup=backup,
        backup_dir=backup_dir,
        as_json=as_json,
        database_url=database_url,
    )


@migrate_app.command("verify")
def verify(
    migration_id: str = typer.Option(..., "--migration-id", help="Run id from a prior migration."),
    as_json: bool = typer.Option(False, "--json"),
    database_url: str | None = DbUrlOption,
) -> None:
    """Re-read a stored migration run's verification report."""
    url = _require_url(database_url)
    engine = create_db_engine(url)
    factory = session_factory(engine)
    try:
        with factory() as s:
            result = verify_migration(s, run_id=migration_id)
    except MigrationError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    finally:
        engine.dispose()

    if as_json:
        typer.echo(_json.dumps(result, indent=2, default=str))
    else:
        typer.echo(f"run {migration_id}: {result['status']}")
        typer.echo(f"  workspace:        {result['workspace_slug']}")
        typer.echo(f"  source manifest:  {result['source_manifest_hash']}")
        typer.echo(f"  backup:           {result['backup_ref']}")
        report = result.get("report") or {}
        if report:
            typer.echo(f"  counts:           {report.get('counts')}")
            typer.echo(f"  hash mismatches:  {len(report.get('hash_mismatches', []))}")
    if result["status"] != "succeeded":
        raise typer.Exit(code=1)


def register(app: typer.Typer) -> None:
    app.add_typer(db_app, name="db")
    app.add_typer(migrate_app, name="migrate")
