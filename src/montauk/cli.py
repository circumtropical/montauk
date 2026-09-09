"""Administrative CLI for a Montauk deployment.

Not a person-record editor -- content changes go through the MCP tools or
the web dashboard. This entry point wires up the operational commands:
``montauk db ...`` (PostgreSQL schema), ``montauk dashboard`` (the web
UI), and ``montauk mcp`` (the agent-facing MCP server).
"""

from __future__ import annotations

import typer

from .cli_phase2 import register as _register

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Montauk relationship-memory admin CLI.",
)
_register(app)


if __name__ == "__main__":
    app()
