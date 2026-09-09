"""Operational CLI: PostgreSQL schema management, the web dashboard, and
the agent-facing MCP server. Mounted on the main ``montauk`` app in cli.py.
"""

from __future__ import annotations

import typer

from .db.engine import DatabaseNotConfigured, create_db_engine, resolve_url, session_factory
from .db.schema_ops import current_revision, downgrade_to, head_revision, upgrade_to_head

db_app = typer.Typer(add_completion=False, no_args_is_help=True, help="PostgreSQL schema management.")

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


def dashboard(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8817, "--port"),
    database_url: str | None = DbUrlOption,
    behind_proxy: bool = typer.Option(
        False,
        "--behind-proxy",
        help="Serve behind an HTTPS reverse proxy (e.g. Caddy): trust X-Forwarded-* "
        "from localhost and mark session cookies Secure.",
    ),
) -> None:
    """Run the Montauk web dashboard (spec 25). Binds to localhost by
    default; put a TLS-terminating reverse proxy in front for anything
    beyond loopback / a private network. It can share one hostname with
    the MCP server -- route ``/mcp`` to the MCP port and everything else
    here (see docs/deploy-phase2-shared-host.md)."""
    import os

    import uvicorn

    from .db.schema_ops import is_up_to_date

    url = _require_url(database_url)
    if not is_up_to_date(url):
        typer.echo("error: schema is not up to date; run `montauk db upgrade` first", err=True)
        raise typer.Exit(code=1)

    loopback = host in ("127.0.0.1", "::1", "localhost")
    os.environ["MONTAUK_DATABASE_URL"] = url
    # Secure cookies whenever TLS is actually in front (proxy or a public bind);
    # honour an explicit MONTAUK_DASHBOARD_SECURE_COOKIES if the operator set one.
    if "MONTAUK_DASHBOARD_SECURE_COOKIES" not in os.environ:
        os.environ["MONTAUK_DASHBOARD_SECURE_COOKIES"] = "0" if (loopback and not behind_proxy) else "1"
    if not loopback and not behind_proxy:
        typer.echo(
            "warning: binding beyond loopback without --behind-proxy -- serve behind "
            "HTTPS so session cookies and credentials are not sent in the clear.",
            err=True,
        )
    uvicorn.run(
        "montauk.web.wsgi:app",
        host=host,
        port=port,
        proxy_headers=behind_proxy,
        forwarded_allow_ips="127.0.0.1,::1" if behind_proxy else None,
    )


def mcp(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8766, "--port"),
    database_url: str | None = DbUrlOption,
) -> None:
    """Run the Phase 2 MCP server: the agent-facing tool surface backed by
    the PostgreSQL store. Binds to localhost by default; put a
    TLS-terminating reverse proxy in front for anything beyond loopback.
    Every request is bearer-authenticated against the workspace's agent
    credentials (Settings -> agent tokens in the dashboard) -- that gate,
    not a Host allowlist, is what keeps the server closed, so DNS-rebinding
    protection (which also breaks the streamable-HTTP SSE flow) stays off."""
    import uvicorn

    from .db.crypto import MasterKeyMissing, SecretBox
    from .db.schema_ops import is_up_to_date
    from .mcp2 import Mcp2Context, build_mcp2_app

    url = _require_url(database_url)
    if not is_up_to_date(url):
        typer.echo("error: schema is not up to date; run `montauk db upgrade` first", err=True)
        raise typer.Exit(code=1)

    try:
        secret_box: SecretBox | None = SecretBox()
    except MasterKeyMissing:
        secret_box = None
        typer.echo(
            "warning: MONTAUK_MASTER_KEY is not set -- briefings will fall back to deterministic "
            "evidence (generated: false).",
            err=True,
        )

    if host not in ("127.0.0.1", "::1", "localhost"):
        typer.echo(
            "warning: binding beyond loopback -- put HTTPS in front; Montauk speaks plain HTTP "
            "and bearer tokens would travel in the clear.",
            err=True,
        )
    ctx = Mcp2Context(session_factory=session_factory(create_db_engine(url)), secret_box=secret_box)
    uvicorn.run(build_mcp2_app(ctx, host=host), host=host, port=port)


def register(app: typer.Typer) -> None:
    app.add_typer(db_app, name="db")
    app.command("dashboard")(dashboard)
    app.command("mcp")(mcp)
