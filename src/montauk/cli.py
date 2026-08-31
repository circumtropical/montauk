"""Administrative CLI (spec section 32). Not a person-record editor --
content changes go through MCP tools or direct Markdown editing.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import typer

from .auth import ENV_VAR_AGENT_TOKEN, CredentialStore, resolve_stdio_identity
from .bootstrap import build_context, reconcile_on_startup
from .config import MontaukConfig, load_config
from .git_snapshot import DailySnapshotScheduler, snapshot_if_changed
from .http_app import build_http_app
from .logging_config import configure_logging
from .markdown_store import MarkdownStore
from .reconciliation import scan_people_directory, write_validation_report
from .server import create_server
from .sqlite_index import SqliteIndex

logger = logging.getLogger("montauk.cli")

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Montauk relationship-memory server admin CLI.")
agents_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Manage agent credentials.")
app.add_typer(agents_app, name="agents")

ConfigOption = typer.Option(None, "--config", help="Path to a YAML config file.")
DataDirOption = typer.Option(None, "--data-dir", help="Deployment data directory (overrides the config's data_dir).")


def _resolve_config(config: Path | None, data_dir: Path | None) -> MontaukConfig:
    if config is not None:
        cfg = load_config(config)
        if data_dir is not None:
            cfg = cfg.model_copy(update={"data_dir": str(data_dir)})
        return cfg
    return MontaukConfig(data_dir=str(data_dir) if data_dir is not None else "./data")


@app.command()
def validate(config: Path | None = ConfigOption, data_dir: Path | None = DataDirOption) -> None:
    """Scan and validate the repository; exit non-zero if unhealthy."""
    cfg = _resolve_config(config, data_dir)
    store = MarkdownStore(cfg.data_dir_path)
    result = scan_people_directory(store)
    write_validation_report(result, store.data_dir)
    typer.echo(f"healthy: {result.healthy}")
    typer.echo(f"valid people: {len(result.valid)}")
    typer.echo(f"errors: {len(result.errors)}")
    typer.echo(f"warnings: {len(result.warnings)}")
    for issue in result.issues:
        typer.echo(f"  [{issue.severity}] {issue.error_type} ({issue.file_path}): {issue.message}")
    if not result.healthy:
        raise typer.Exit(code=1)


@app.command()
def status(config: Path | None = ConfigOption, data_dir: Path | None = DataDirOption) -> None:
    """Print operational health status (counts and last reconciliation time)."""
    cfg = _resolve_config(config, data_dir)
    store = MarkdownStore(cfg.data_dir_path)
    sqlite_index = SqliteIndex(cfg.data_dir_path / "index" / "relationships.sqlite")
    result = scan_people_directory(store)
    write_validation_report(result, store.data_dir)
    typer.echo(f"healthy: {result.healthy}")
    typer.echo(f"valid people: {len(result.valid)}")
    typer.echo(f"errors: {len(result.errors)}")
    typer.echo(f"warnings: {len(result.warnings)}")
    typer.echo(f"last_reconciliation_at: {sqlite_index.get_meta('last_reconciliation_at')}")


@app.command(name="rebuild-index")
def rebuild_index(config: Path | None = ConfigOption, data_dir: Path | None = DataDirOption) -> None:
    """Delete-and-rebuild the derived SQLite index from canonical Markdown."""
    cfg = _resolve_config(config, data_dir)
    store = MarkdownStore(cfg.data_dir_path)
    sqlite_index = SqliteIndex(cfg.data_dir_path / "index" / "relationships.sqlite")
    result = scan_people_directory(store)
    sqlite_index.rebuild_from_scan(store, result)
    typer.echo(f"rebuilt index: {len(result.valid)} people indexed")


@app.command(name="rebuild-vectors")
def rebuild_vectors(
    config: Path | None = ConfigOption,
    data_dir: Path | None = DataDirOption,
    model: str | None = typer.Option(None, "--model", help="Override the configured embedding model."),
) -> None:
    """Delete-and-rebuild the derived semantic/vector index from canonical Markdown."""
    from .embeddings.local import LocalEmbeddingProvider
    from .semantic_index import SemanticIndex

    cfg = _resolve_config(config, data_dir)
    store = MarkdownStore(cfg.data_dir_path)
    provider = LocalEmbeddingProvider(model_name=model or cfg.embedding.model)
    semantic_index = SemanticIndex(cfg.data_dir_path / "index" / "vectors", provider)
    result = scan_people_directory(store)
    semantic_index.rebuild_from_scan(result)
    typer.echo(f"rebuilt vector index: {semantic_index.chunk_count()} chunks")


@app.command(name="git-snapshot")
def git_snapshot_cmd(config: Path | None = ConfigOption, data_dir: Path | None = DataDirOption) -> None:
    """Run one daily-snapshot check-and-commit-if-changed immediately."""
    cfg = _resolve_config(config, data_dir)
    committed = snapshot_if_changed(cfg.data_dir_path)
    typer.echo("committed" if committed else "no changes; nothing to commit")


@agents_app.command("list")
def agents_list(config: Path | None = ConfigOption, data_dir: Path | None = DataDirOption) -> None:
    """List agent credentials (never prints tokens)."""
    cfg = _resolve_config(config, data_dir)
    credential_store = CredentialStore(cfg.data_dir_path / "auth" / "credentials.sqlite")
    records = credential_store.list_agents()
    if not records:
        typer.echo("no agents registered")
        return
    for record in records:
        state = f"revoked at {record.revoked_at}" if record.revoked else "active"
        typer.echo(f"{record.agent_id}\t{record.name}\t{record.role}\t{record.created_at}\t{state}")


@agents_app.command("create")
def agents_create(
    name: str = typer.Option(..., "--name"),
    role: str = typer.Option(..., "--role", help="read_only or read_write"),
    config: Path | None = ConfigOption,
    data_dir: Path | None = DataDirOption,
) -> None:
    """Create a new agent credential. The raw token is printed once and never stored in retrievable form."""
    if role not in ("read_only", "read_write"):
        typer.echo("error: --role must be 'read_only' or 'read_write'", err=True)
        raise typer.Exit(code=1)
    cfg = _resolve_config(config, data_dir)
    credential_store = CredentialStore(cfg.data_dir_path / "auth" / "credentials.sqlite")
    record, token = credential_store.create_agent(name, role)  # type: ignore[arg-type]
    typer.echo(f"Created agent {record.agent_id!r} (role={record.role}).")
    typer.echo("Token (shown once -- store it now; it cannot be retrieved again):")
    typer.echo(token)


@agents_app.command("revoke")
def agents_revoke(
    agent_id: str, config: Path | None = ConfigOption, data_dir: Path | None = DataDirOption
) -> None:
    """Revoke an agent credential."""
    cfg = _resolve_config(config, data_dir)
    credential_store = CredentialStore(cfg.data_dir_path / "auth" / "credentials.sqlite")
    if credential_store.revoke_agent(agent_id):
        typer.echo(f"revoked {agent_id}")
    else:
        typer.echo(f"error: agent {agent_id!r} not found or already revoked", err=True)
        raise typer.Exit(code=1)


@app.command(name="config-check")
def config_check(config: Path = typer.Option(..., "--config", help="Path to a YAML config file.")) -> None:
    """Validate a config file and print the resolved settings."""
    try:
        cfg = load_config(config)
    except Exception as exc:
        typer.echo(f"config invalid: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"data_dir: {cfg.data_dir_path}")
    typer.echo(f"transport: {cfg.transport.mode} ({cfg.transport.host}:{cfg.transport.port})")
    typer.echo(f"git.enabled: {cfg.git.enabled} (daily at {cfg.git.daily_snapshot_time})")
    typer.echo(f"search.semantic_enabled: {cfg.search.semantic_enabled}")
    typer.echo(f"embedding: {cfg.embedding.provider}/{cfg.embedding.model}")
    typer.echo(f"logging: level={cfg.logging.level}, retain {cfg.logging.retention_days} days")
    typer.echo("config OK")


@app.command()
def serve(
    config: Path | None = ConfigOption,
    data_dir: Path | None = DataDirOption,
    transport: str | None = typer.Option(None, "--transport", help="Override config transport.mode (stdio|remote)."),
) -> None:
    """Run the long-running Montauk MCP server (the only command that stays running)."""
    cfg = _resolve_config(config, data_dir)
    configure_logging(cfg.logging, log_dir=cfg.data_dir_path / "logs")
    mode = transport or cfg.transport.mode
    asyncio.run(_serve_async(cfg, mode))


def _maybe_warn_plaintext_remote(mode: str, host: str) -> None:
    """ctx.credential_store is always set for `serve` (build_context is
    called with with_auth=True unconditionally), so BearerAuthMiddleware
    already rejects every request without a valid, non-revoked
    credential regardless of bind address -- spec section 30's "never
    expose an unauthenticated server" is structurally guaranteed, not
    something to re-check here. What *isn't* guaranteed is transport
    encryption: Montauk speaks plain HTTP only, so binding all
    interfaces without a TLS-terminating reverse proxy in front sends
    bearer tokens in the clear over the network.
    """
    if mode == "remote" and host in ("0.0.0.0", "::"):
        logger.warning(
            "remote transport is bound to %s (all interfaces); Montauk speaks plain HTTP only -- "
            "put a TLS-terminating reverse proxy in front before exposing this beyond localhost",
            host,
        )


async def _serve_async(cfg: MontaukConfig, mode: str) -> None:
    ctx = build_context(cfg, with_auth=True)
    reconcile_on_startup(ctx)

    if mode == "stdio":
        token = os.environ.get(ENV_VAR_AGENT_TOKEN)
        ctx.stdio_identity = resolve_stdio_identity(ctx.credential_store, token) if ctx.credential_store else None
    else:
        _maybe_warn_plaintext_remote(mode, cfg.transport.host)

    mcp_server = create_server(context=ctx)

    scheduler = None
    if cfg.git.enabled:
        scheduler = DailySnapshotScheduler(ctx.store.data_dir, daily_time=cfg.daily_snapshot_time_parsed)
        scheduler.start()

    try:
        if mode == "stdio":
            await mcp_server.run_stdio_async()
        else:
            import uvicorn

            http_app = build_http_app(mcp_server, credential_store=ctx.credential_store, host=cfg.transport.host)
            uvicorn_config = uvicorn.Config(
                http_app, host=cfg.transport.host, port=cfg.transport.port, log_level=cfg.logging.level.lower()
            )
            await uvicorn.Server(uvicorn_config).serve()
    finally:
        if scheduler is not None:
            await scheduler.stop()


if __name__ == "__main__":
    app()
