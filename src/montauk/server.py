"""MCP server registration for Montauk (spec sections 24, 25).

This module builds the MCPServer instance and publishes the server-level
usage instructions (spec section 25.2). Tool registration is layered on
in later modules/steps so the Agent Integration Contract (instructions +
per-tool descriptions/schemas) stays close to the server construction
code, as spec section 25 asks.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

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

RETRIEVAL
- Prefer targeted retrieval for a specific question.
- Request the full record only when the complete record is useful.

CORRECTIONS
- Correct or remove information that is discovered to be wrong. Do not preserve misinformation as an active superseded fact solely for history; Git provides edit history.

BOUNDARIES
- Only people are first-class records in Phase 1.
- This server stores and retrieves relationship memory; the calling assistant is responsible for interpretation, conversational disambiguation, and prose generation.
"""


def create_server(*, name: str = "montauk") -> MCPServer:
    """Construct the bare Montauk MCPServer with its server-level
    instructions. Tool/resource registration is added by callers in
    later steps once the write queue and stores it depends on exist."""
    return MCPServer(name=name, instructions=SERVER_INSTRUCTIONS)
