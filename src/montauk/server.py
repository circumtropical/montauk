"""MCP server registration for Montauk (spec sections 24, 25).

This module builds the MCPServer instance and publishes the server-level
usage instructions (spec section 25.2). Tool registration is layered on
in later modules/steps so the Agent Integration Contract (instructions +
per-tool descriptions/schemas) stays close to the server construction
code, as spec section 25 asks.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from .tools_core import MontaukContext, register_batch_tools, register_core_tools
from .tools_ops import register_ops_tools

SERVER_INSTRUCTIONS = """\
Montauk is the user's persistent relationship memory about people they know.

IDENTITY
- If you already have a person_id, use it directly.
- If person_id is unknown, search for the person before creating or modifying a record.
- Search may return multiple plausible people. Do not guess when identity is ambiguous; surface the candidates and ask the user to clarify.
- Do not create a duplicate person merely because identity is uncertain.

RECORDING INFORMATION
- Store concise facts and interaction summaries, not raw conversations, emails, transcripts, or message dumps.
- Record a meaningful interaction even if it produced no new facts.
- When one event produces several related changes for one person, prefer the atomic one-person batch update.
- Use high confidence for directly stated or strongly supported facts; use medium or low confidence for genuine inference or uncertainty.

RECORD SCOPING
- Keep each record scoped to the person of record.
- Mention another person only when that person has a direct relationship or interaction with the person of record, or when the reference is necessary to understand a fact directly about the person of record.
- People appearing in the same conversation or source material are not necessarily related. Never infer a relationship from co-occurrence.
- When one source discusses several unrelated people, separate the information by subject and update each person independently.
- Omit unrelated surrounding context. If relevance is uncertain, omit the reference or ask the user to clarify.

RETRIEVAL
- Prefer targeted retrieval for a specific question.
- Request the full record only when the complete record is useful.

CORRECTIONS
- Correct or remove information that is discovered to be wrong. Do not preserve misinformation as an active superseded fact solely for history; Git provides edit history.

BOUNDARIES
- Only people are first-class records in Phase 1.
- This server stores and retrieves relationship memory; the calling assistant is responsible for interpretation, conversational disambiguation, and prose generation.
"""


def create_server(*, name: str = "montauk", context: MontaukContext | None = None) -> MCPServer:
    """Construct the Montauk MCPServer with its server-level instructions.
    When `context` (store/index/write_queue) is given, the core
    identity/read/write tools are registered against it; further tool
    groups (batch, birthdays/cadence/archive/health, search, auth) are
    layered on by their own registration functions in later steps."""
    server = MCPServer(name=name, instructions=SERVER_INSTRUCTIONS)
    if context is not None:
        register_core_tools(server, context)
        register_batch_tools(server, context)
        register_ops_tools(server, context)
    return server
