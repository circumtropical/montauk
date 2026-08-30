import asyncio
import re
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport

from montauk.markdown_store import MarkdownStore
from montauk.server import create_server
from montauk.sqlite_index import SqliteIndex
from montauk.tools_core import MontaukContext
from montauk.write_queue import WriteQueue


def _build_context(tmp_path: Path) -> MontaukContext:
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
    ctx = _build_context(tmp_path)
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


def _local_id_num(local_id: str) -> int:
    return int(re.search(r"-(\d+)$", local_id).group(1))


class TestToolRegistration:
    @pytest.mark.asyncio
    async def test_fourteen_core_tools_registered(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            tools = (await session.list_tools()).tools
            names = {t.name for t in tools}
            assert names == {
                "search_people",
                "get_person",
                "get_full_record",
                "get_facts",
                "get_interactions",
                "create_person",
                "add_fact",
                "update_fact",
                "remove_fact",
                "record_interaction",
                "update_contact_details",
                "update_summary",
                "set_birthday",
                "set_contact_cadence",
            }

    @pytest.mark.asyncio
    async def test_every_tool_has_a_nonempty_description(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            tools = (await session.list_tools()).tools
            for tool in tools:
                assert tool.description and len(tool.description) > 20, tool.name


class TestCreatePerson:
    @pytest.mark.asyncio
    async def test_create_and_get(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            result = await call(session, "create_person", name="Lisa Simpson", summary="Plays sax.")
            assert result["person_id"] == "lisa-simpson"
            assert result["index_update_status"] == "ok"

            person = await call(session, "get_person", person_id="lisa-simpson")
            assert person["name"] == "Lisa Simpson"
            assert person["summary"] == "Plays sax."
            assert "facts" not in person  # PersonCore excludes facts/interactions

    @pytest.mark.asyncio
    async def test_duplicate_name_gets_suffixed_id(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            r1 = await call(session, "create_person", name="Gil Gunderson")
            r2 = await call(session, "create_person", name="Gil Gunderson")
            assert r1["person_id"] == "gil-gunderson"
            assert r2["person_id"] == "gil-gunderson-2"

    @pytest.mark.asyncio
    async def test_invalid_cadence_is_a_validation_error(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            text = await call_expecting_error(
                session, "create_person", name="Bad Cadence", desired_contact_cadence_days=-5
            )
            assert "VALIDATION_ERROR" in text


class TestGetPersonErrors:
    @pytest.mark.asyncio
    async def test_not_found(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            text = await call_expecting_error(session, "get_person", person_id="nobody")
            assert "NOT_FOUND" in text


class TestFacts:
    @pytest.mark.asyncio
    async def test_add_get_update_remove_fact(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")

            add_result = await call(
                session, "add_fact", person_id="homer-simpson", category="Family", text="Married to Marge."
            )
            assert add_result["changed_ids"] == ["fact-1"]

            facts = await call(session, "get_facts", person_id="homer-simpson")
            assert len(facts) == 1
            assert facts[0]["text"] == "Married to Marge."
            assert facts[0]["confidence"] == "high"

            await call(
                session,
                "update_fact",
                person_id="homer-simpson",
                fact_id="fact-1",
                text="Married to Marge Simpson.",
                confidence="medium",
            )
            facts = await call(session, "get_facts", person_id="homer-simpson")
            assert facts[0]["text"] == "Married to Marge Simpson."
            assert facts[0]["confidence"] == "medium"

            await call(session, "remove_fact", person_id="homer-simpson", fact_id="fact-1")
            facts = await call(session, "get_facts", person_id="homer-simpson")
            assert facts == []

    @pytest.mark.asyncio
    async def test_get_facts_filtered_by_category(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            await call(session, "add_fact", person_id="homer-simpson", category="Family", text="A")
            await call(session, "add_fact", person_id="homer-simpson", category="Interests", text="B")

            family_only = await call(session, "get_facts", person_id="homer-simpson", category="Family")
            assert [f["text"] for f in family_only] == ["A"]

    @pytest.mark.asyncio
    async def test_invalid_category_is_validation_error(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            text = await call_expecting_error(
                session, "add_fact", person_id="homer-simpson", category="Not A Category", text="x"
            )
            assert "VALIDATION_ERROR" in text

    @pytest.mark.asyncio
    async def test_remove_nonexistent_fact_is_not_found(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            text = await call_expecting_error(session, "remove_fact", person_id="homer-simpson", fact_id="fact-99")
            assert "NOT_FOUND" in text

    @pytest.mark.asyncio
    async def test_invalid_update_leaves_file_untouched(self, tmp_path):
        async with running_session(tmp_path) as (session, ctx):
            await call(session, "create_person", name="Homer Simpson")
            await call(session, "add_fact", person_id="homer-simpson", category="Family", text="Original text.")
            before = ctx.store.person_path("homer-simpson").read_text()

            await call_expecting_error(
                session, "update_fact", person_id="homer-simpson", fact_id="fact-1", category="Nonexistent Category"
            )

            after = ctx.store.person_path("homer-simpson").read_text()
            assert before == after


class TestInteractions:
    @pytest.mark.asyncio
    async def test_record_and_get_interactions_most_recent_first(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            await call(session, "record_interaction", person_id="homer-simpson", date="2024", summary="old")
            await call(session, "record_interaction", person_id="homer-simpson", date="2026-08-20", summary="new")

            interactions = await call(session, "get_interactions", person_id="homer-simpson")
            assert [i["summary"] for i in interactions] == ["new", "old"]

    @pytest.mark.asyncio
    async def test_get_interactions_respects_limit(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            for year in ("2020", "2021", "2022"):
                await call(session, "record_interaction", person_id="homer-simpson", date=year)

            limited = await call(session, "get_interactions", person_id="homer-simpson", limit=1)
            assert len(limited) == 1
            assert limited[0]["date"] == "2022"

    @pytest.mark.asyncio
    async def test_record_interaction_worth_it_even_with_no_new_facts(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            result = await call(session, "record_interaction", person_id="homer-simpson", date="2026-08-20")
            assert result["changed_ids"] == ["int-1"]


class TestStructuredFieldUpdates:
    @pytest.mark.asyncio
    async def test_update_contact_details_partial(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            await call(session, "update_contact_details", person_id="homer-simpson", emails=["homer@example.com"])
            await call(session, "update_contact_details", person_id="homer-simpson", phones=["+1-555-0100"])

            person = await call(session, "get_person", person_id="homer-simpson")
            assert person["contact"]["emails"] == ["homer@example.com"]
            assert person["contact"]["phones"] == ["+1-555-0100"]

    @pytest.mark.asyncio
    async def test_update_summary(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            await call(session, "update_summary", person_id="homer-simpson", summary="Updated.")
            person = await call(session, "get_person", person_id="homer-simpson")
            assert person["summary"] == "Updated."

    @pytest.mark.asyncio
    async def test_set_and_clear_birthday(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            await call(session, "set_birthday", person_id="homer-simpson", birthday="1956-05-12")
            person = await call(session, "get_person", person_id="homer-simpson")
            assert person["birthday"] == "1956-05-12"

            await call(session, "set_birthday", person_id="homer-simpson", birthday=None)
            person = await call(session, "get_person", person_id="homer-simpson")
            assert person["birthday"] is None

    @pytest.mark.asyncio
    async def test_set_contact_cadence(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            await call(session, "set_contact_cadence", person_id="homer-simpson", desired_contact_cadence_days=30)
            person = await call(session, "get_person", person_id="homer-simpson")
            assert person["desired_contact_cadence_days"] == 30


class TestGetFullRecord:
    @pytest.mark.asyncio
    async def test_returns_canonical_markdown_text(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            await call(session, "add_fact", person_id="homer-simpson", category="Family", text="Married to Marge.")

            record = await call(session, "get_full_record", person_id="homer-simpson")
            assert isinstance(record, str)
            assert "id: homer-simpson" in record
            assert "## Family" in record
            assert "Married to Marge." in record


class TestSearchPeople:
    @pytest.mark.asyncio
    async def test_matches_by_name_substring(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            await call(session, "create_person", name="Marge Simpson")

            result = await call(session, "search_people", query="homer")
            assert [c["person_id"] for c in result["candidates"]] == ["homer-simpson"]
            assert "matches" in result["candidates"][0]["match_evidence"][0]

    @pytest.mark.asyncio
    async def test_multiple_candidates_returned_for_shared_name(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Gil Gunderson")
            await call(session, "create_person", name="Gil Gunderson")

            result = await call(session, "search_people", query="Gil")
            assert {c["person_id"] for c in result["candidates"]} == {"gil-gunderson", "gil-gunderson-2"}

    @pytest.mark.asyncio
    async def test_no_match_returns_empty_candidates(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            result = await call(session, "search_people", query="nonexistent")
            assert result["candidates"] == []


class TestConcurrentWrites:
    @pytest.mark.asyncio
    async def test_concurrent_add_fact_calls_do_not_corrupt_the_file(self, tmp_path):
        async with running_session(tmp_path) as (session, ctx):
            await call(session, "create_person", name="Homer Simpson")

            async def add(i: int):
                return await call(
                    session, "add_fact", person_id="homer-simpson", category="General Notes", text=f"note {i}"
                )

            results = await asyncio.gather(*(add(i) for i in range(10)))
            changed_ids = sorted({r["changed_ids"][0] for r in results}, key=_local_id_num)
            assert changed_ids == [f"fact-{i}" for i in range(1, 11)]

            # The file must parse cleanly and contain exactly the 10 facts,
            # with no duplicated or dropped IDs from a lost update.
            person = ctx.store.read_person("homer-simpson")
            assert sorted((f.id for f in person.facts), key=_local_id_num) == [f"fact-{i}" for i in range(1, 11)]
