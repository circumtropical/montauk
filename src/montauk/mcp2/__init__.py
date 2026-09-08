"""Phase 2 MCP server: the agent-facing tool surface, backed by the
PostgreSQL store (spec 24, 25).

This supersedes the Phase 1 Markdown MCP server (``montauk serve``). It
speaks the same streamable-HTTP transport with the same bearer-token
gate, but every tool reads and writes the canonical Postgres store that
the dashboard also uses, and the retrieval surface leads with
``prepare_person_briefing`` (a Montauk-LLM report) rather than raw facts.
"""

from .context import Mcp2Context
from .server import build_mcp2_app, create_mcp2_server

__all__ = ["Mcp2Context", "build_mcp2_app", "create_mcp2_server"]
