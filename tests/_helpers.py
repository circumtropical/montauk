"""Shared test helpers for exercising the MCP tool surface over a real
in-memory client<->server protocol session."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from mcp import ClientSession
from mcp.client._memory import InMemoryTransport

from montauk.markdown_store import MarkdownStore
from montauk.server import create_server
from montauk.sqlite_index import SqliteIndex
from montauk.tools_core import MontaukContext
from montauk.write_queue import WriteQueue


def build_context(tmp_path: Path) -> MontaukContext:
    data_dir = tmp_path / "data"
    store = MarkdownStore(data_dir)
    sqlite_index = SqliteIndex(data_dir / "index" / "relationships.sqlite")
    write_queue = WriteQueue(data_dir)
    return MontaukContext(store=store, sqlite_index=sqlite_index, write_queue=write_queue)


@asynccontextmanager
async def running_session(tmp_path: Path):
    """A real MCP client<->server session over an in-memory transport, so
    tool calls go through full protocol dispatch (schema validation,
    ToolError -> is_error conversion) rather than calling handlers directly."""
    ctx = build_context(tmp_path)
    server = create_server(context=ctx)
    async with InMemoryTransport(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            yield session, ctx


def _unwrap(structured_content):
    # Tools whose declared return type isn't a JSON object (str, list[...])
    # get auto-wrapped by the SDK as {"result": ...} at the protocol level,
    # since structuredContent must be an object; unwrap it back for tests.
    if isinstance(structured_content, dict) and structured_content.keys() == {"result"}:
        return structured_content["result"]
    return structured_content


async def call(session: ClientSession, tool_name: str, **kwargs):
    result = await session.call_tool(tool_name, kwargs)
    if result.is_error:
        text = "\n".join(block.text for block in result.content if hasattr(block, "text"))
        raise AssertionError(f"tool {tool_name!r} returned an error: {text}")
    if result.structured_content is not None:
        return _unwrap(result.structured_content)
    return "\n".join(block.text for block in result.content if hasattr(block, "text"))


async def call_expecting_error(session: ClientSession, tool_name: str, **kwargs) -> str:
    result = await session.call_tool(tool_name, kwargs)
    assert result.is_error, f"expected tool {tool_name!r} to return an error, but it succeeded"
    return "\n".join(block.text for block in result.content if hasattr(block, "text"))
