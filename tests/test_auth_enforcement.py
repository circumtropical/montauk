"""End-to-end auth enforcement through the actual MCP tool surface.

The in-memory transport never populates Context.headers, so these tests
exercise the stdio identity-resolution fallback (ctx.stdio_identity) --
the same path a real `montauk serve` (stdio mode) would use after
resolving MONTAUK_AGENT_TOKEN once at startup. resolve_http_identity
itself is covered directly in test_auth.py, and the streamable-HTTP
transport's own Context.headers wiring is exercised once that transport
is stood up.
"""

import pytest

from _helpers import call, call_expecting_error, running_session
from montauk.auth import CredentialStore


def _store(tmp_path) -> CredentialStore:
    return CredentialStore(tmp_path / "data" / "auth" / "credentials.sqlite")


class TestNoAuthWiredIsPermissive:
    @pytest.mark.asyncio
    async def test_mutations_work_when_credential_store_is_none(self, tmp_path):
        async with running_session(tmp_path) as (session, _ctx):
            result = await call(session, "create_person", name="Homer Simpson")
            assert result["person_id"] == "P0001"


class TestReadOnlyRejectedOnMutations:
    @pytest.mark.asyncio
    async def test_read_only_agent_cannot_create_person(self, tmp_path):
        credential_store = _store(tmp_path)
        _record, token = credential_store.create_agent("read-only-agent", "read_only")
        async with running_session(tmp_path, credential_store=credential_store) as (session, ctx):
            ctx.stdio_identity = credential_store.verify_token(token)
            text = await call_expecting_error(session, "create_person", name="Homer Simpson")
            assert "PERMISSION_DENIED" in text

    @pytest.mark.asyncio
    async def test_read_only_agent_can_still_read(self, tmp_path):
        credential_store = _store(tmp_path)
        _writer_record, writer_token = credential_store.create_agent("writer", "read_write")
        _reader_record, reader_token = credential_store.create_agent("reader", "read_only")
        async with running_session(tmp_path, credential_store=credential_store) as (session, ctx):
            ctx.stdio_identity = credential_store.verify_token(writer_token)
            await call(session, "create_person", name="Homer Simpson")

            ctx.stdio_identity = credential_store.verify_token(reader_token)
            person = await call(session, "get_person", person_id="P0001")
            assert person["name"] == "Homer Simpson"

    @pytest.mark.asyncio
    async def test_read_only_rejected_on_every_mutation_tool(self, tmp_path):
        credential_store = _store(tmp_path)
        _writer_record, writer_token = credential_store.create_agent("writer", "read_write")
        _reader_record, reader_token = credential_store.create_agent("reader", "read_only")

        async with running_session(tmp_path, credential_store=credential_store) as (session, ctx):
            ctx.stdio_identity = credential_store.verify_token(writer_token)
            await call(session, "create_person", name="Homer Simpson")
            await call(session, "add_fact", person_id="P0001", category="Family", text="x")

            ctx.stdio_identity = credential_store.verify_token(reader_token)
            mutation_calls = [
                ("create_person", {"name": "Someone Else"}),
                ("add_fact", {"person_id": "P0001", "category": "Family", "text": "y"}),
                ("update_fact", {"person_id": "P0001", "fact_id": "fact-1", "text": "z"}),
                ("remove_fact", {"person_id": "P0001", "fact_id": "fact-1"}),
                ("record_interaction", {"person_id": "P0001", "date": "2026"}),
                ("update_contact_details", {"person_id": "P0001", "emails": ["a@example.com"]}),
                ("update_summary", {"person_id": "P0001", "summary": "new"}),
                ("set_birthday", {"person_id": "P0001", "birthday": "1990-01-01"}),
                ("set_contact_cadence", {"person_id": "P0001", "desired_contact_cadence_days": 30}),
                (
                    "update_person_batch",
                    {"person_id": "P0001", "operations": [{"op": "update_summary", "summary": "x"}]},
                ),
                ("archive_person", {"person_id": "P0001"}),
            ]
            for tool_name, kwargs in mutation_calls:
                text = await call_expecting_error(session, tool_name, **kwargs)
                assert "PERMISSION_DENIED" in text, f"{tool_name} did not reject a read_only agent"


class TestRevokedTokenRejected:
    @pytest.mark.asyncio
    async def test_revoked_agent_cannot_write(self, tmp_path):
        credential_store = _store(tmp_path)
        record, token = credential_store.create_agent("writer", "read_write")
        credential_store.revoke_agent(record.agent_id)

        async with running_session(tmp_path, credential_store=credential_store) as (session, ctx):
            # verify_token itself returns None for a revoked token, exactly
            # as it would if a real transport re-verified on every call.
            ctx.stdio_identity = credential_store.verify_token(token)
            assert ctx.stdio_identity is None
            text = await call_expecting_error(session, "create_person", name="Homer Simpson")
            assert "PERMISSION_DENIED" in text


class TestValidReadWriteSucceeds:
    @pytest.mark.asyncio
    async def test_read_write_agent_can_mutate(self, tmp_path):
        credential_store = _store(tmp_path)
        _record, token = credential_store.create_agent("writer", "read_write")

        async with running_session(tmp_path, credential_store=credential_store) as (session, ctx):
            ctx.stdio_identity = credential_store.verify_token(token)
            result = await call(session, "create_person", name="Homer Simpson")
            assert result["person_id"] == "P0001"
            await call(session, "add_fact", person_id="P0001", category="Family", text="x")
            await call(session, "archive_person", person_id="P0001")


class TestNoCredentialPresented:
    @pytest.mark.asyncio
    async def test_missing_identity_is_permission_denied_when_auth_is_wired(self, tmp_path):
        credential_store = _store(tmp_path)
        async with running_session(tmp_path, credential_store=credential_store) as (session, ctx):
            assert ctx.stdio_identity is None  # nothing resolved -- no token ever set
            text = await call_expecting_error(session, "create_person", name="Homer Simpson")
            assert "PERMISSION_DENIED" in text
