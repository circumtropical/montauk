"""Protocol-conformance spot check against the actual packaged
entrypoint: everything else in this suite talks to the server through
an in-memory transport (InMemoryTransport), which never touches process
boundaries, stdio framing, or the installed console-script entry point.
This test runs the real `montauk` executable as a subprocess -- the
same way a real MCP host would launch it -- and drives it with a
genuine mcp.ClientSession over stdio.
"""

import shutil
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _montauk_executable() -> str:
    # Same venv pytest is running in (installed in editable/dev mode via
    # `uv sync`), so this is the actual packaged console-script entry
    # point from pyproject.toml's [project.scripts], not a re-import.
    candidate = Path(sys.executable).parent / "montauk"
    if candidate.exists():
        return str(candidate)
    found = shutil.which("montauk")
    if found:
        return found
    pytest.skip("montauk console script not found on PATH; run `uv sync` first")


@pytest.mark.asyncio
async def test_real_subprocess_stdio_serve_end_to_end(tmp_path):
    params = StdioServerParameters(
        command=_montauk_executable(),
        args=["serve", "--data-dir", str(tmp_path / "data")],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init_result = await session.initialize()
            assert init_result.server_info.name == "montauk"
            assert init_result.instructions

            tools = await session.list_tools()
            assert len(tools.tools) == 23

            # No auth configured (no MONTAUK_AGENT_TOKEN in the child's
            # env), so a mutation must be rejected -- proves auth
            # enforcement holds through the real process boundary too.
            create_result = await session.call_tool("create_person", {"name": "Subprocess Test Person"})
            assert create_result.is_error
            error_text = "\n".join(b.text for b in create_result.content if hasattr(b, "text"))
            assert "PERMISSION_DENIED" in error_text

            # Reads work fine without auth.
            search_result = await session.call_tool("search_people", {"query": "nobody"})
            assert not search_result.is_error
