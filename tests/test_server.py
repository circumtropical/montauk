import pytest
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport

from montauk.server import SERVER_INSTRUCTIONS, create_server


class TestServerSkeleton:
    def test_instructions_are_non_empty(self):
        server = create_server()
        assert server.instructions
        assert server.instructions == SERVER_INSTRUCTIONS

    def test_instructions_mention_core_workflow_concepts(self):
        # Sanity check that the published text actually carries the
        # workflow guidance spec section 25.2 asks for, not just any string.
        text = SERVER_INSTRUCTIONS
        for phrase in ("search for the person", "one-person batch", "high confidence", "Git provides edit history"):
            assert phrase in text

    def test_server_name(self):
        server = create_server(name="montauk-test")
        assert server.name == "montauk-test"

    @pytest.mark.asyncio
    async def test_instructions_are_exposed_via_protocol_initialize(self):
        # A real client<->server handshake over an in-memory transport --
        # confirms the instructions are actually discoverable through the
        # MCP protocol itself, not just present as a Python attribute.
        server = create_server()
        async with InMemoryTransport(server) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                result = await session.initialize()
                assert result.instructions == SERVER_INSTRUCTIONS

    @pytest.mark.asyncio
    async def test_no_tools_registered_yet(self):
        server = create_server()
        async with InMemoryTransport(server) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert tools.tools == []
