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

IDENTITY AND NAMES
- If you already have a person_id, use it directly.
- A person_id is a permanent system-generated identity such as P0001. Never derive it from a name, change it after creation, or reuse it.
- A person's name is the best currently known display name. It may be partial, approximate, misspelled, or later corrected.
- Never archive and recreate a person merely to correct or complete their name. Use update_person_name and keep useful former or alternate names as aliases.
- If person_id is unknown, search for the person before creating or modifying a record.
- Search may return multiple plausible people. Do not guess when identity is ambiguous; surface the candidates and ask the user to clarify.
- Do not create a duplicate person merely because identity is uncertain. Similar or identical names do not by themselves identify the same person; ask before merging identities or moving information between people.

RECORDING INFORMATION
- Store concise facts and interaction summaries, not raw conversations, emails, transcripts, or message dumps.
- Record a meaningful interaction even if it produced no new facts.
- When one event produces several related changes for one person, prefer the atomic one-person batch update.
- Use high confidence for directly stated or strongly supported facts; use medium or low confidence for genuine inference or uncertainty.

INTERACTION CORRECTIONS
- Use update_interaction when an interaction occurred but its participants or other recorded details are inaccurate.
- If an interaction was attributed to the wrong person, correct its participant (move_to_person_id) so the incorrect person retains no interaction record or searchable association.
- Use remove_interaction only when the interaction record itself is erroneous, never occurred, or duplicates another record.
- Correcting inaccurate data is not erasing history. Never remove an accurate interaction merely because it is old, inconvenient, sensitive, or no longer relevant.

RECORD SCOPING
- Keep each record scoped to the person of record.
- Mention another person only when that person has a direct relationship or interaction with the person of record, or when the reference is necessary to understand a fact directly about the person of record.
- People appearing in the same conversation or source material are not necessarily related. Never infer a relationship from co-occurrence.
- When one source discusses several unrelated people, separate the information by subject and update each person independently.
- Omit unrelated surrounding context. If relevance is uncertain, omit the reference or ask the user to clarify.

RETRIEVAL
- Resolve the person first (search_people), then use prepare_person_context with the actual question or task as `purpose` to get a bounded set of relevant facts, relationships, and interactions.
- Treat that result as evidence from stored memory, not as Montauk's advice or a generated answer. Montauk does not give advice, recommendations, compatibility judgments, or quotations -- you do.
- If the response is truncated or lacks an exact detail, narrow the purpose or retrieve the cited records with get_facts / get_interactions.
- Retrieve a complete raw record (get_full_record) only for explicit review, export, or maintenance.
- Do not invent names, quotations, dates, or facts that the evidence does not contain.

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
