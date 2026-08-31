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
from .embeddings.local import DEFAULT_MODEL_NAME
from .git_snapshot import DailySnapshotScheduler, create_initial_commit, ensure_git_repo, snapshot_if_changed
from .http_app import build_http_app, build_transport_security
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


_STARTER_CONFIG = """\
# Montauk deployment configuration (spec section 31).
# Non-secret settings only -- agent credentials are managed via
# `montauk agents create` and never belong in this file.

data_dir: {data_dir}

transport:
  # "stdio" for a single local agent host launching `montauk serve`
  # directly; "remote" for authenticated HTTPS access by multiple agents
  # (put a TLS-terminating reverse proxy in front).
  mode: stdio
  host: 127.0.0.1
  port: 8765
  # For mode: remote behind a reverse proxy, set this to the URL agents
  # connect to. It lets the proxy forward requests without rewriting the
  # Host header. Leave unset for stdio.
  # public_url: https://montauk.example.com

git:
  enabled: true
  # 24-hour local time; at most one automatic commit per day, only if
  # canonical data changed. Never auto-pushes.
  daily_snapshot_time: "03:00"

search:
  semantic_enabled: true
  max_candidates: 5
  similarity_threshold: 0.35

embedding:
  provider: local
  model: {model}

logging:
  level: INFO
  retention_days: 30
"""

_DATA_REPO_README = """\
# Montauk relationship data

This is the **private** canonical data store for a Montauk deployment
(relationship memory for personal agents). It contains personal information
about real people.

**Keep this repository private.** The Montauk source code lives in a separate
repository; this one never should be made public.

## Layout

- `people/`  -- one Markdown file per active person; canonical and authoritative
- `archive/` -- people archived out of active use
- `config.yaml` -- this deployment's non-secret settings

Excluded from version control (see `.gitignore`): `index/` (derived SQLite and
vector indexes, rebuilt on demand), `auth/` (agent credentials -- secret),
`logs/`, and `validation-report.json`.

## Recovery

The derived indexes are disposable. After cloning this repository:

    montauk rebuild-index   --data-dir .
    montauk rebuild-vectors --data-dir .

regenerates them from the Markdown files.
"""


@app.command()
def init(config: Path | None = ConfigOption, data_dir: Path | None = DataDirOption) -> None:
    """Scaffold a new deployment: create the data directory and a starter
    config, and initialise (but never push) a private git repository for the
    canonical Markdown data.
    """
    if config is None and data_dir is None:
        typer.echo(
            "error: pass --data-dir PATH (or --config PATH) so init knows where to create the deployment",
            err=True,
        )
        raise typer.Exit(code=1)

    cfg = _resolve_config(config, data_dir)
    target = cfg.data_dir_path

    # 1. Canonical directory layout (MarkdownStore creates people/ and archive/).
    MarkdownStore(target)
    for sub in ("people", "archive"):
        keep = target / sub / ".gitkeep"
        if not keep.exists():
            keep.write_text("", encoding="utf-8")

    # 2. Starter config, unless the operator pointed at their own with --config.
    wrote_config = False
    if config is None:
        config_path = target / "config.yaml"
        if not config_path.exists():
            config_path.write_text(
                _STARTER_CONFIG.format(data_dir=target, model=DEFAULT_MODEL_NAME), encoding="utf-8"
            )
            wrote_config = True

    # 3. Data-repo README.
    readme_path = target / "README.md"
    if not readme_path.exists():
        readme_path.write_text(_DATA_REPO_README, encoding="utf-8")

    # 4. Private git repo on branch main, with an initial commit so it can be
    #    pushed to a remote. Montauk itself never adds a remote or pushes.
    ensure_git_repo(target)
    committed = create_initial_commit(
        target,
        message="Initialise Montauk data repository",
        extra_paths=("README.md", "config.yaml", "people/.gitkeep", "archive/.gitkeep"),
    )

    typer.echo(f"Initialised Montauk deployment at {target}")
    typer.echo("  people/, archive/   canonical Markdown (git-tracked)")
    if wrote_config:
        typer.echo("  config.yaml         starter config written (edit to taste)")
    typer.echo("  .gitignore          excludes index/, auth/, logs/")
    typer.echo(
        f"  git repo            {'initial commit created on main' if committed else 'already had commits'}"
    )
    typer.echo("")
    typer.echo("Next steps:")
    typer.echo("  1. Create an EMPTY private repo on your git host (no README / license / .gitignore).")
    typer.echo(f"  2. cd {target}")
    typer.echo("     git remote add origin git@github.com:<you>/<your-data-repo>.git")
    typer.echo("     git push -u origin main")
    typer.echo("  3. `montauk serve` commits new/changed person files locally each day but never")
    typer.echo("     pushes. To mirror to your remote, add a cron entry, e.g.:")
    typer.echo(f"       15 3 * * *  cd {target} && git push -q origin main")
    typer.echo("  4. Create an agent credential:")
    typer.echo(f"     montauk agents create --name my-agent --role read_write --data-dir {target}")


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
    if cfg.transport.public_url:
        typer.echo(f"transport.public_url: {cfg.transport.public_url}")
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


def _maybe_warn_plaintext_remote(mode: str, host: str, public_url: str | None = None) -> None:
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
    if mode == "remote" and not public_url:
        logger.info(
            "transport.public_url is not set; DNS-rebinding protection is disabled for the remote "
            "transport (every request is still bearer-authenticated). Set it to the URL agents "
            "connect to (e.g. https://montauk.example.com) to restrict the accepted Host header."
        )


async def _serve_async(cfg: MontaukConfig, mode: str) -> None:
    ctx = build_context(cfg, with_auth=True)
    reconcile_on_startup(ctx)

    if mode == "stdio":
        token = os.environ.get(ENV_VAR_AGENT_TOKEN)
        ctx.stdio_identity = resolve_stdio_identity(ctx.credential_store, token) if ctx.credential_store else None
    else:
        _maybe_warn_plaintext_remote(mode, cfg.transport.host, cfg.transport.public_url)

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

            http_app = build_http_app(
                mcp_server,
                credential_store=ctx.credential_store,
                host=cfg.transport.host,
                transport_security=build_transport_security(
                    mode=mode,
                    host=cfg.transport.host,
                    port=cfg.transport.port,
                    public_url=cfg.transport.public_url,
                ),
            )
            uvicorn_config = uvicorn.Config(
                http_app, host=cfg.transport.host, port=cfg.transport.port, log_level=cfg.logging.level.lower()
            )
            await uvicorn.Server(uvicorn_config).serve()
    finally:
        if scheduler is not None:
            await scheduler.stop()


if __name__ == "__main__":
    app()
