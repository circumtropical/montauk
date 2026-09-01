"""Spec section 25/37 acceptance criterion: a newly connected MCP-capable
agent must be able to discover the full tool catalog and intended
interaction patterns through the protocol itself -- server instructions
plus complete, explicit per-tool descriptions/schemas -- without any
Montauk-specific hard-coded integration.
"""

import pytest
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport

from _helpers import running_session
from montauk.server import SERVER_INSTRUCTIONS, create_server


class TestServerLevelDiscovery:
    @pytest.mark.asyncio
    async def test_instructions_are_discoverable_over_the_wire_and_substantial(self):
        # Full protocol round trip (not just the Python attribute), same
        # pattern as test_server.py -- re-verified here alongside the
        # tool-catalog checks since both are part of the one discovery
        # acceptance criterion.
        server = create_server()
        async with InMemoryTransport(server) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                result = await session.initialize()
        assert result.instructions == SERVER_INSTRUCTIONS
        assert len(SERVER_INSTRUCTIONS) > 200
        assert "IDENTITY" in SERVER_INSTRUCTIONS
        assert "BOUNDARIES" in SERVER_INSTRUCTIONS


class TestToolCatalogCompleteness:
    @pytest.mark.asyncio
    async def test_every_tool_has_a_name_description_and_object_input_schema(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            tools = (await session.list_tools()).tools
            assert len(tools) == 27
            for tool in tools:
                assert tool.name
                assert tool.description and len(tool.description) >= 20, f"{tool.name} description too thin"
                assert tool.input_schema.get("type") == "object", f"{tool.name} input schema not an object"

    @pytest.mark.asyncio
    async def test_mutation_tools_declare_structured_output_schemas(self, tmp_path):
        mutation_tools = {
            "create_person",
            "add_fact",
            "update_fact",
            "remove_fact",
            "record_interaction",
            "update_interaction",
            "remove_interaction",
            "update_contact_details",
            "update_summary",
            "update_person_name",
            "set_birthday",
            "set_contact_cadence",
            "update_person_batch",
            "archive_person",
        }
        async with running_session(tmp_path) as (session, _ctx):
            tools = {t.name: t for t in (await session.list_tools()).tools}
            for name in mutation_tools:
                schema = tools[name].output_schema
                assert schema is not None, f"{name} has no structured output schema"
                assert schema.get("type") == "object"

    @pytest.mark.asyncio
    async def test_required_person_id_params_are_marked_required_in_schema(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            tools = {t.name: t for t in (await session.list_tools()).tools}
            for name in ("get_person", "get_facts", "get_interactions", "archive_person"):
                schema = tools[name].input_schema
                assert "person_id" in schema.get("required", []), f"{name} doesn't require person_id"

    @pytest.mark.asyncio
    async def test_search_people_description_documents_ambiguity_behavior(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            tools = {t.name: t for t in (await session.list_tools()).tools}
            description = tools["search_people"].description.lower()
            assert "ambiguous" in description or "multiple" in description
            assert "clarify" in description or "guess" in description

    @pytest.mark.asyncio
    async def test_create_person_description_warns_to_search_first(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            tools = {t.name: t for t in (await session.list_tools()).tools}
            description = tools["create_person"].description.lower()
            assert "search" in description

    @pytest.mark.asyncio
    async def test_update_person_batch_description_documents_atomicity(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            tools = {t.name: t for t in (await session.list_tools()).tools}
            description = tools["update_person_batch"].description.lower()
            assert "atomic" in description
            assert "validat" in description  # "validated"/"validates"
            assert "one person" in description or "exactly one" in description

    @pytest.mark.asyncio
    async def test_get_full_record_description_warns_about_context_cost(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            tools = {t.name: t for t in (await session.list_tools()).tools}
            description = tools["get_full_record"].description.lower()
            assert "context" in description or "prefer targeted" in description
            assert "review, export, or maintenance" in description  # progressive disclosure

    @pytest.mark.asyncio
    async def test_prepare_person_context_description_teaches_progressive_disclosure(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            tools = {t.name: t for t in (await session.list_tools()).tools}
            d = tools["prepare_person_context"].description.lower()
            assert "purpose" in d
            assert "evidence" in d and "not" in d  # evidence, not a generated answer
            assert "verbatim" in d
            assert "invent" in d or "do not invent" in d
            assert "brief" in d and "standard" in d and "comprehensive" in d
            assert "max_tokens" in d
            assert "truncat" in d
            schema = tools["prepare_person_context"].input_schema
            assert "person_id" in schema.get("required", [])
            assert "purpose" in schema.get("required", [])

    @pytest.mark.asyncio
    async def test_prepare_person_context_declares_structured_output(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            tools = {t.name: t for t in (await session.list_tools()).tools}
            schema = tools["prepare_person_context"].output_schema
            assert schema is not None and schema.get("type") == "object"

    def test_server_instructions_teach_purpose_specific_retrieval(self):
        assert "prepare_person_context" in SERVER_INSTRUCTIONS
        assert "evidence" in SERVER_INSTRUCTIONS.lower()
        assert "not as Montauk's advice" in SERVER_INSTRUCTIONS or "not Montauk's advice" in SERVER_INSTRUCTIONS

    @pytest.mark.asyncio
    async def test_archive_person_description_clarifies_reversibility(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            tools = {t.name: t for t in (await session.list_tools()).tools}
            description = tools["archive_person"].description.lower()
            assert "reversible" in description
            assert "not" in description and "erasure" in description

    @pytest.mark.asyncio
    async def test_record_interaction_description_covers_no_new_facts_case(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            tools = {t.name: t for t in (await session.list_tools()).tools}
            description = tools["record_interaction"].description.lower()
            assert "no new facts" in description or "even when" in description

    @pytest.mark.asyncio
    async def test_no_tool_name_collides_with_another(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            names = [t.name for t in (await session.list_tools()).tools]
            assert len(names) == len(set(names))
