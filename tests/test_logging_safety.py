"""Spec section 28: default logs must never carry sensitive fact text,
full person records, contact details, or raw request payloads -- only
identifiers, counts, timestamps, and status.
"""

import logging

import pytest
from _helpers import call, running_session

SENSITIVE_SUMMARY = "Secretly considering leaving their spouse; told us in confidence."
SENSITIVE_FACT_TEXT = "Diagnosed with a serious illness; asked us not to tell anyone."
SENSITIVE_EMAIL = "very.private.address@example.com"
SENSITIVE_PHONE = "+1-555-0199-secret"


@pytest.fixture
def captured_logs(caplog):
    caplog.set_level(logging.DEBUG, logger="montauk")
    return caplog


class TestNoSensitiveContentInLogs:
    @pytest.mark.asyncio
    async def test_create_person_with_sensitive_summary(self, tmp_path, captured_logs):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson", summary=SENSITIVE_SUMMARY)
        assert SENSITIVE_SUMMARY not in captured_logs.text

    @pytest.mark.asyncio
    async def test_add_fact_with_sensitive_text(self, tmp_path, captured_logs):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            await call(session, "add_fact", person_id="P0001", category="General Notes", text=SENSITIVE_FACT_TEXT)
        assert SENSITIVE_FACT_TEXT not in captured_logs.text

    @pytest.mark.asyncio
    async def test_contact_details_never_logged(self, tmp_path, captured_logs):
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            await call(
                session,
                "update_contact_details",
                person_id="P0001",
                emails=[SENSITIVE_EMAIL],
                phones=[SENSITIVE_PHONE],
            )
        assert SENSITIVE_EMAIL not in captured_logs.text
        assert SENSITIVE_PHONE not in captured_logs.text

    @pytest.mark.asyncio
    async def test_validation_error_path_does_not_leak_sensitive_text(self, tmp_path, captured_logs):
        # Even a *rejected* write with sensitive-looking content in its
        # arguments must not have that content echoed into the logs.
        async with running_session(tmp_path) as (session, _ctx):
            await call(session, "create_person", name="Homer Simpson")
            await session.call_tool(
                "add_fact",
                {"person_id": "P0001", "category": "Not A Real Category", "text": SENSITIVE_FACT_TEXT},
            )
        assert SENSITIVE_FACT_TEXT not in captured_logs.text

    @pytest.mark.asyncio
    async def test_degraded_index_log_line_names_only_the_person_id(self, tmp_path, captured_logs):
        from montauk.markdown_store import MarkdownStore
        from montauk.models import Person
        from montauk.tools_core import MontaukContext, _write_and_index
        from montauk.write_queue import WriteQueue

        class ExplodingSqliteIndex:
            def upsert_person(self, *args, **kwargs):
                raise RuntimeError("simulated index failure")

        store = MarkdownStore(tmp_path / "data")
        ctx = MontaukContext(
            store=store, sqlite_index=ExplodingSqliteIndex(), write_queue=WriteQueue(tmp_path / "data")
        )
        person = Person(id="P0001", name="Homer Simpson", summary=SENSITIVE_SUMMARY)

        status = _write_and_index(ctx, person)

        assert status == "degraded"
        assert SENSITIVE_SUMMARY not in captured_logs.text
        assert "P0001" in captured_logs.text  # the identifier itself IS expected
