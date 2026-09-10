"""Phase 2 MCP server: the agent-facing tool surface over Postgres, with
briefing-first retrieval, drill-down, bearer auth, and workspace scoping.

Tool calls go through real streamable-HTTP protocol dispatch (schema
validation, ToolError -> is_error, header auth) against an in-process
ASGI transport.
"""

from __future__ import annotations

import socket
import threading
import time
from contextlib import asynccontextmanager, contextmanager

import httpx2
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from montauk.db.repositories import Actor, PeopleRepository, WorkspaceScope
from montauk.llm.providers.fake import FakeProvider
from montauk.mcp2 import Mcp2Context, build_mcp2_app
from montauk.models import Fact, Interaction, Person
from montauk.services import briefing, model_config
from montauk.services.agent_credentials import create_credential


def _token(db_session, workspace, capabilities=("memory_read", "memory_write")) -> str:
    _cred, raw = create_credential(
        db_session,
        workspace_id=workspace.id,
        name=f"agent-{'-'.join(capabilities)}",
        capabilities=list(capabilities),
    )
    return raw


def _person(db_session, workspace, **overrides) -> str:
    scope = WorkspaceScope(db_session, workspace.id, Actor("owner", "owner@example.com"))
    defaults = dict(
        id="P0001",
        name="Amanda Dwelley",
        summary="Friend from the sailing club.",
        birthday="1984-05-11",
        company="Midcoast Land Trust",
        facts=[
            Fact(
                id="fact-1",
                category="Work & Education",
                text="Runs land-protection projects in Midcoast Maine.",
            ),
            Fact(id="fact-2", category="Interests", text="Races a Rhodes 19 on Wednesday nights."),
            Fact(id="fact-3", category="Family", text="Two kids, both in middle school."),
        ],
        interactions=[
            Interaction(
                id="int-1", date="2024-06-02", channel="dinner", summary="Talked about a new easement deal."
            ),
        ],
    )
    defaults.update(overrides)
    PeopleRepository(scope).create(Person(**defaults))
    db_session.flush()
    return defaults["id"]


def _configure_fake_model(db_session, workspace, secret_box, monkeypatch, text: str) -> None:
    model_config.save(
        db_session,
        workspace.id,
        "summarization",
        provider_type="claude_cli",
        model="claude-haiku-4-5",
        secret_box=secret_box,
    )
    monkeypatch.setattr(
        briefing,
        "build_provider",
        lambda cfg: FakeProvider(model="claude-haiku-4-5", responder=lambda s, p: text),
    )


@contextmanager
def _serve(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture
def mcp_url(session_maker, secret_box):
    app = build_mcp2_app(Mcp2Context(session_factory=session_maker, secret_box=secret_box))
    with _serve(app) as url:
        yield url


@asynccontextmanager
async def _client(url: str, token: str | None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    http = httpx2.AsyncClient(base_url=url, headers=headers)
    async with http:
        async with streamable_http_client(f"{url}/mcp", http_client=http) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


async def _call(session: ClientSession, name: str, **kwargs):
    import json

    result = await session.call_tool(name, kwargs)
    if result.is_error:
        text = "\n".join(b.text for b in result.content if hasattr(b, "text"))
        raise AssertionError(f"tool {name!r} errored: {text}")
    sc = result.structured_content
    if isinstance(sc, dict) and set(sc) == {"result"}:
        return sc["result"]
    if sc is not None:
        return sc
    text = "\n".join(b.text for b in result.content if hasattr(b, "text"))
    try:
        return json.loads(text)
    except ValueError:
        return text


async def _call_error(session: ClientSession, name: str, **kwargs) -> str:
    result = await session.call_tool(name, kwargs)
    assert result.is_error, f"expected {name!r} to error"
    return "\n".join(b.text for b in result.content if hasattr(b, "text"))


class TestAuth:
    async def test_missing_token_is_rejected_at_the_transport(self, mcp_url):
        with pytest.raises(Exception):  # noqa: B017 - initialize fails on the 401
            async with _client(mcp_url, token=None):
                pass

    async def test_read_only_token_cannot_write(self, db_session, workspace, secret_box, mcp_url):
        _person(db_session, workspace)
        token = _token(db_session, workspace, capabilities=("memory_read",))
        db_session.commit()
        async with _client(mcp_url, token) as s:
            assert await _call(s, "get_person", person_id="P0001")
            err = await _call_error(s, "add_fact", person_id="P0001", category="Interests", text="x")
        assert "memory_write" in err


class TestBriefingFirst:
    async def test_briefing_then_drill_down(self, db_session, workspace, secret_box, mcp_url, monkeypatch):
        _person(db_session, workspace)
        _configure_fake_model(
            db_session,
            workspace,
            secret_box,
            monkeypatch,
            "Amanda runs land-protection work in Midcoast Maine and races a Rhodes 19.\n"
            "SOURCE_REFS: fact-1, fact-2",
        )
        token = _token(db_session, workspace)
        db_session.commit()

        async with _client(mcp_url, token) as s:
            brief = await _call(
                s, "prepare_person_briefing", person_id="P0001", purpose="brief me before I call Amanda"
            )
            assert brief["generated"] is True
            assert "land-protection" in brief["briefing"]
            assert "evidence" not in brief  # not duplicated by default
            assert set(brief["source_refs"]) == {"fact-1", "fact-2"}

            sources = await _call(
                s, "get_context_sources", person_id="P0001", source_refs=brief["source_refs"]
            )
            assert sources["missing_refs"] == []
            assert {x["ref"] for x in sources["sources"]} == {"fact-1", "fact-2"}

    async def test_narrow_question_bypasses_the_model(
        self, db_session, workspace, secret_box, mcp_url, monkeypatch
    ):
        _person(db_session, workspace)
        calls: list[tuple[str, str]] = []
        _configure_fake_model(db_session, workspace, secret_box, monkeypatch, "should not be used")
        monkeypatch.setattr(
            briefing,
            "build_provider",
            lambda cfg: FakeProvider(responder=lambda s, p: calls.append((s, p)) or "x"),
        )
        token = _token(db_session, workspace)
        db_session.commit()

        async with _client(mcp_url, token) as s:
            out = await _call(
                s, "prepare_person_briefing", person_id="P0001", purpose="what is Amanda's birthday?"
            )
        assert out["generated"] is False
        assert out["answer"] == "1984-05-11"
        assert calls == []

    async def test_no_model_returns_deterministic_evidence(self, db_session, workspace, secret_box, mcp_url):
        _person(db_session, workspace)
        token = _token(db_session, workspace)
        db_session.commit()
        async with _client(mcp_url, token) as s:
            out = await _call(s, "prepare_person_briefing", person_id="P0001", purpose="brief me on Amanda")
        assert out["generated"] is False and out["status"] == "llm_unavailable"
        assert out["evidence"]["facts"]


class TestScoping:
    async def test_token_only_sees_its_own_workspace(
        self, db_session, workspace, other_workspace, secret_box, mcp_url
    ):
        _person(db_session, workspace)  # P0001 in Alpha
        _person(db_session, other_workspace, id="P0001", name="Someone Beta", facts=[], interactions=[])
        beta_token = _token(db_session, other_workspace)
        db_session.commit()
        async with _client(mcp_url, beta_token) as s:
            person = await _call(s, "get_person", person_id="P0001")
            assert person["name"] == "Someone Beta"  # Beta's record, never Alpha's


class TestWrites:
    async def test_write_roundtrip_and_briefing_reflects_it(
        self, db_session, workspace, secret_box, mcp_url, monkeypatch
    ):
        _person(db_session, workspace)
        token = _token(db_session, workspace)
        db_session.commit()

        async with _client(mcp_url, token) as s:
            r = await _call(
                s,
                "add_fact",
                person_id="P0001",
                category="Life Events",
                text="Just started a two-year term on the town planning board.",
            )
            assert r["changed_ids"] == ["fact-4"]

            r2 = await _call(
                s,
                "record_interaction",
                person_id="P0001",
                date="2024-07-15",
                summary="Coffee about the board role.",
            )
            assert r2["changed_ids"] == ["int-2"]

            facts = await _call(s, "get_facts", person_id="P0001", category="Life Events")
            assert facts[0]["text"].startswith("Just started a two-year term")

            batch = await _call(
                s,
                "update_person_batch",
                person_id="P0001",
                operations=[
                    {"op": "set_contact_cadence", "desired_contact_cadence_days": 30},
                    {"op": "add_fact", "category": "Interests", "text": "Getting into cross-country skiing."},
                ],
            )
            assert "fact-5" in batch["changed_ids"]

        # a fresh read sees the committed writes
        db_session.expire_all()
        scope = WorkspaceScope(db_session, workspace.id, Actor("owner", "o"))
        row = PeopleRepository(scope).require("P0001")
        assert row.desired_contact_cadence_days == 30
        assert len(row.facts) == 5 and len(row.interactions) == 2

    async def test_search_and_not_found(self, db_session, workspace, secret_box, mcp_url):
        _person(db_session, workspace)
        token = _token(db_session, workspace)
        db_session.commit()
        async with _client(mcp_url, token) as s:
            found = await _call(s, "search_people", query="Dwelley")
            assert found["candidates"][0]["person_id"] == "P0001"
            err = await _call_error(s, "get_person", person_id="P9999")
        assert "P9999" in err


class TestConnectorHealth:
    async def test_reports_unconfigured_with_no_secrets(self, db_session, workspace, secret_box, mcp_url):
        token = _token(db_session, workspace, capabilities=("memory_read",))
        db_session.commit()
        async with _client(mcp_url, token) as s:
            out = await _call(s, "get_connector_health")
        assert out["connectors"][0] == {
            "provider": "whatsapp",
            "status": "unconfigured",
            "enabled_threads": 0,
        }

    async def test_reflects_a_connected_account(self, db_session, workspace, secret_box, mcp_url):
        from montauk.db import models as orm

        db_session.add(
            orm.ConnectorAccount(
                workspace_id=workspace.id,
                provider="whatsapp",
                status="connected",
                self_phone="+15550001111",
                encrypted_session="v1:secret",
            )
        )
        token = _token(db_session, workspace, capabilities=("memory_read",))
        db_session.commit()
        async with _client(mcp_url, token) as s:
            out = await _call(s, "get_connector_health")
        health = out["connectors"][0]
        assert health["status"] == "connected" and health["connected_as"] == "+15550001111"
        assert "secret" not in str(out) and "encrypted_session" not in str(out)
