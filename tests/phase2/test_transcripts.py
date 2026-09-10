"""WhatsApp transcript import + extraction (spec 14-17): idempotent
archiving, incremental re-import, and model-driven fact extraction at
automatically-extracted authority."""

from __future__ import annotations

import pytest

from montauk.db import models as orm
from montauk.db.repositories import PeopleRepository
from montauk.llm.base import LLMRateLimited
from montauk.llm.providers.fake import FakeProvider
from montauk.models import Person
from montauk.services import extraction, model_config, transcripts
from montauk.services.whatsapp import parse_whatsapp_export

# --- a small fictional export -------------------------------------------------

DAY1 = "\n".join(
    [
        "[2026-03-01, 9:00:00 AM] Alex: morning! how was the move to Portland?",
        "[2026-03-01, 9:01:10 AM] Robin Vega: so good. I started at Coastal Studios last week as a lead designer",
        "[2026-03-01, 9:01:40 AM] Robin Vega: my sister is visiting from Denver next month",
        "[2026-03-01, 9:02:00 AM] Robin Vega: ‎<Media omitted>",
        "[2026-03-01, 9:02:30 AM] Alex: nice, congrats on the job",
    ]
)
DAY2 = "\n".join(
    [
        "[2026-03-05, 6:00:00 PM] Robin Vega: went bouldering at the new gym, it's great",
        "[2026-03-05, 6:05:00 PM] Alex: we should go sometime",
    ]
)
HEADER = "[2026-03-01, 8:59:00 AM] Alex: Messages and calls are end-to-end encrypted.\n"


def _export(*days: str) -> bytes:
    return (HEADER + "\n".join(days) + "\n").encode("utf-8")


# --- parser -----------------------------------------------------------------


class TestParser:
    def test_ios_format_multiline_media_and_system(self):
        t = parse_whatsapp_export(_export(DAY1))
        assert t.participants == ["Alex", "Robin Vega"]
        media = [m for m in t.messages if m.media_omitted]
        system = [m for m in t.messages if m.is_system]
        assert len(media) == 1 and len(system) == 1
        robin_job = next(m for m in t.messages if m.sender == "Robin Vega" and "Coastal" in m.text)
        assert "lead designer" in robin_job.text

    def test_android_format_and_continuation(self):
        raw = b"3/1/26, 9:00 AM - Alex: hi\n3/1/26, 9:01 AM - Robin: multi\nline message\n"
        t = parse_whatsapp_export(raw)
        assert [m.sender for m in t.messages] == ["Alex", "Robin"]
        assert t.messages[1].text == "multi\nline message"

    def test_day_first_detected_from_a_day_over_12(self):
        raw = b"25/12/2026, 10:00 - Sam: yo\n26/12/2026, 11:00 - Sam: hi\n"
        t = parse_whatsapp_export(raw)
        assert t.messages[0].sent_at.month == 12 and t.messages[0].sent_at.day == 25


# --- import + dedup -------------------------------------------------------


class TestImport:
    def _import(self, db_session, scope, content: bytes, name="chat.txt"):
        r = transcripts.import_whatsapp(db_session, scope, filename=name, content=content, created_by=None)
        db_session.flush()
        return r

    def test_archives_messages_and_registers_participants(self, db_session, scope):
        r = self._import(db_session, scope, _export(DAY1))
        assert r.new_messages == 6 and r.duplicate_messages == 0
        assert set(r.participants) == {"Alex", "Robin Vega"}

        parts = db_session.query(orm.SourceParticipant).all()
        assert {p.role for p in parts} == {"unmapped"}
        msgs = db_session.query(orm.SourceMessage).all()
        awaiting = [m for m in msgs if m.processing_status == "awaiting_processing"]
        skipped = [m for m in msgs if m.processing_status == "skipped"]
        assert len(awaiting) == 4  # media + system are archived but skipped
        assert len(skipped) == 2

    def test_reimporting_the_same_file_adds_nothing(self, db_session, scope):
        self._import(db_session, scope, _export(DAY1))
        again = self._import(db_session, scope, _export(DAY1))
        assert again.new_messages == 0 and again.already_imported_identical_file
        assert db_session.query(orm.SourceThread).count() == 1
        assert db_session.query(orm.SourceMessage).count() == 6

    def test_a_longer_export_adds_only_the_tail(self, db_session, scope):
        self._import(db_session, scope, _export(DAY1))
        bigger = self._import(db_session, scope, _export(DAY1, DAY2))
        assert bigger.new_messages == 2  # just DAY2
        assert bigger.duplicate_messages == 6
        assert db_session.query(orm.SourceMessage).count() == 8

    def test_empty_or_unparseable_file_is_rejected(self, db_session, scope):
        with pytest.raises(transcripts.TranscriptError):
            self._import(db_session, scope, b"not a whatsapp export at all")

    def test_guesses_participants_on_import(self, db_session, scope):
        robin = PeopleRepository(scope).create(Person(id="P0042", name="Robin Vega"))
        db_session.flush()
        export = (
            b"[2026-03-01, 9:00:00 AM] You: hi\n"
            b"[2026-03-01, 9:01:00 AM] Robin Vega: hello\n"
            b"[2026-03-01, 9:02:00 AM] Mystery Person: who am i\n"
        )
        self._import(db_session, scope, export)
        roles = {p.display_name: (p.role, p.person_id) for p in db_session.query(orm.SourceParticipant).all()}
        assert roles["You"][0] == "owner"
        assert roles["Robin Vega"] == ("person", robin.id)  # name match
        assert roles["Mystery Person"][0] == "unmapped"  # no match

    def test_guesses_owner_as_the_other_side_of_a_pair(self, db_session, scope):
        PeopleRepository(scope).create(Person(id="P0001", name="Amanda Dwelley"))
        db_session.flush()
        export = b"[2026-03-01, 9:00:00 AM] Joshua: hey\n[2026-03-01, 9:01:00 AM] Amanda Dwelley: hi there\n"
        self._import(db_session, scope, export)
        roles = {p.display_name: p.role for p in db_session.query(orm.SourceParticipant).all()}
        assert roles == {"Joshua": "owner", "Amanda Dwelley": "person"}


# --- extraction ---------------------------------------------------------


def _person(scope, pid, name) -> orm.Person:
    row = PeopleRepository(scope).create(Person(id=pid, name=name))
    scope.session.flush()
    return row


def _map(db_session, thread_id, display_name, *, role, person=None):
    p = (
        db_session.query(orm.SourceParticipant)
        .filter_by(thread_id=thread_id, display_name=display_name)
        .one()
    )
    p.role = role
    p.person_id = person.id if person else None
    db_session.flush()


_MODEL_JSON = (
    '{"facts": ['
    '{"person_id": "P0007", "category": "Work & Education", "text": "Started as a lead designer at Coastal Studios.", "confidence": "high", "date": "2026-02"},'
    '{"person_id": "P0007", "category": "Family", "text": "Has a sister in Denver.", "confidence": "high", "date": null}'
    '], "interactions": {"P0007": "Caught up about the move to Portland and the new design job."}}'
)


class TestExtraction:
    @pytest.fixture
    def thread(self, db_session, scope):
        r = transcripts.import_whatsapp(
            db_session, scope, filename="c.txt", content=_export(DAY1, DAY2), created_by=None
        )
        db_session.flush()
        return r.thread_id

    async def test_no_model_configured(self, db_session, scope, thread, secret_box):
        _person(scope, "P0007", "Robin Vega")
        _map(db_session, thread, "Robin Vega", role="person", person=PeopleRepository(scope).require("P0007"))
        res = await extraction.run_extraction(db_session, scope, thread_id=thread, secret_box=secret_box)
        assert res.status == "model_unavailable"

    async def test_no_mapped_person(self, db_session, scope, workspace, thread, secret_box):
        model_config.save(
            db_session,
            workspace.id,
            "extraction",
            provider_type="claude_cli",
            model="m",
            secret_box=secret_box,
        )
        db_session.flush()
        res = await extraction.run_extraction(db_session, scope, thread_id=thread, secret_box=secret_box)
        assert res.status == "no_mapping"

    async def test_extracts_facts_and_one_interaction_per_day(
        self, db_session, scope, workspace, thread, secret_box, monkeypatch
    ):
        robin = _person(scope, "P0007", "Robin Vega")
        _map(db_session, thread, "Robin Vega", role="person", person=robin)
        _map(db_session, thread, "Alex", role="owner")
        model_config.save(
            db_session,
            workspace.id,
            "extraction",
            provider_type="claude_cli",
            model="m",
            secret_box=secret_box,
        )
        db_session.flush()

        calls: list[str] = []

        def responder(system, prompt):
            calls.append(prompt)
            return _MODEL_JSON if "2026-03-01" in prompt else '{"facts": [], "interactions": {}}'

        monkeypatch.setattr(
            extraction, "build_provider", lambda cfg: FakeProvider(model="x", responder=responder)
        )
        res = await extraction.run_extraction(db_session, scope, thread_id=thread, secret_box=secret_box)
        db_session.flush()

        assert res.status == "ok"
        assert res.days_processed == 2 and res.facts_added == 2 and res.interactions_touched == 1

        domain = PeopleRepository(scope).require("P0007")
        db_session.refresh(domain)
        facts = domain.facts
        assert len(facts) == 2 and all(f.authority == "automatically_extracted" for f in facts)
        assert any("Coastal Studios" in f.text for f in facts)
        interactions = [i for i in domain.interactions if i.channel == "whatsapp"]
        assert len(interactions) == 1 and interactions[0].date_text == "2026-03-01"

        # nothing pending now
        pending = (
            db_session.query(orm.SourceMessage)
            .filter_by(thread_id=thread, processing_status="awaiting_processing")
            .count()
        )
        assert pending == 0
        rerun = await extraction.run_extraction(db_session, scope, thread_id=thread, secret_box=secret_box)
        assert rerun.status == "nothing_pending"

    async def test_reextraction_does_not_duplicate_facts(
        self, db_session, scope, workspace, thread, secret_box, monkeypatch
    ):
        robin = _person(scope, "P0007", "Robin Vega")
        _map(db_session, thread, "Robin Vega", role="person", person=robin)
        model_config.save(
            db_session,
            workspace.id,
            "extraction",
            provider_type="claude_cli",
            model="m",
            secret_box=secret_box,
        )
        db_session.flush()
        monkeypatch.setattr(
            extraction,
            "build_provider",
            lambda cfg: FakeProvider(
                model="x",
                responder=lambda s, p: _MODEL_JSON if "03-01" in p else '{"facts":[],"interactions":{}}',
            ),
        )
        await extraction.run_extraction(db_session, scope, thread_id=thread, secret_box=secret_box)
        # re-import the same content (0 new) then re-run: no new facts because the
        # messages are already 'processed', and identical fact text is skipped anyway.
        transcripts.import_whatsapp(
            db_session, scope, filename="c.txt", content=_export(DAY1, DAY2), created_by=None
        )
        db_session.flush()
        res = await extraction.run_extraction(db_session, scope, thread_id=thread, secret_box=secret_box)
        assert res.status == "nothing_pending"
        assert PeopleRepository(scope).require("P0007").facts.__len__() == 2

    async def test_day_cap_leaves_a_backlog(self, db_session, scope, workspace, secret_box, monkeypatch):
        days = [
            f"[2026-04-{d:02d}, 9:00:00 AM] Robin Vega: something happened on day {d}" for d in range(1, 6)
        ]
        r = transcripts.import_whatsapp(
            db_session, scope, filename="c.txt", content=_export(*days), created_by=None
        )
        db_session.flush()
        robin = _person(scope, "P0007", "Robin Vega")
        _map(db_session, r.thread_id, "Robin Vega", role="person", person=robin)
        model_config.save(
            db_session,
            workspace.id,
            "extraction",
            provider_type="claude_cli",
            model="m",
            secret_box=secret_box,
        )
        db_session.flush()
        monkeypatch.setattr(
            extraction,
            "build_provider",
            lambda cfg: FakeProvider(model="x", responder=lambda s, p: '{"facts":[],"interactions":{}}'),
        )
        res = await extraction.run_extraction(
            db_session, scope, thread_id=r.thread_id, secret_box=secret_box, max_days=3
        )
        assert res.status == "partial" and res.days_processed == 3 and res.awaiting_remaining == 2

    async def test_budget_block(self, db_session, scope, workspace, thread, secret_box, monkeypatch):
        robin = _person(scope, "P0007", "Robin Vega")
        _map(db_session, thread, "Robin Vega", role="person", person=robin)
        model_config.save(
            db_session,
            workspace.id,
            "extraction",
            provider_type="claude_cli",
            model="m",
            secret_box=secret_box,
        )
        settings = db_session.get(orm.WorkspaceSettings, workspace.id)
        settings.llm_processing_paused = True
        db_session.flush()
        res = await extraction.run_extraction(db_session, scope, thread_id=thread, secret_box=secret_box)
        assert res.status == "budget_exceeded"

    async def test_provider_error_is_partial(
        self, db_session, scope, workspace, thread, secret_box, monkeypatch
    ):
        robin = _person(scope, "P0007", "Robin Vega")
        _map(db_session, thread, "Robin Vega", role="person", person=robin)
        model_config.save(
            db_session,
            workspace.id,
            "extraction",
            provider_type="claude_cli",
            model="m",
            secret_box=secret_box,
        )
        db_session.flush()
        monkeypatch.setattr(
            extraction, "build_provider", lambda cfg: FakeProvider(fail_with=LLMRateLimited("slow"))
        )
        res = await extraction.run_extraction(db_session, scope, thread_id=thread, secret_box=secret_box)
        assert res.status == "error" and res.facts_added == 0
        assert db_session.query(orm.LLMUsageEvent).filter_by(ok=False).count() == 1


# --- dashboard --------------------------------------------------------


class TestDashboard:
    def _setup(self, client):
        client.post(
            "/setup",
            data={
                "email": "o@example.com",
                "password": "correct-horse-staple",
                "password_confirm": "correct-horse-staple",
                "workspace_name": "Personal",
                "deployment_profile": "private",
                "public_url": "",
            },
        )

    def _csrf(self, client, path="/"):
        html = client.get(path).text
        marker = 'name="_csrf" value="'
        i = html.index(marker) + len(marker)
        return html[i : html.index('"', i)]

    def test_import_disabled_without_extraction_model(self, client):
        self._setup(client)
        html = client.get("/transcripts").text
        assert "An extraction model is required" in html
        assert 'action="/transcripts/import"' not in html

    def test_status_endpoint_reports_counts(self, client, db_session):
        self._setup(client)
        from montauk.db.repositories import Actor, WorkspaceScope
        from montauk.services.workspace import get_or_create_workspace

        ws = get_or_create_workspace(db_session, "Personal")
        db_session.flush()
        scope = WorkspaceScope(db_session, ws.id, Actor("owner", "o"))
        imp = transcripts.import_whatsapp(
            db_session, scope, filename="c.txt", content=_export(DAY1, DAY2), created_by=None
        )
        db_session.commit()

        s = client.get(f"/transcripts/{imp.thread_id}/extract/status").json()
        assert s["active"] is False
        assert s["processed"] == 0 and s["remaining"] == s["total"] > 0

    def test_upload_guesses_participants_then_extract(self, client, session_maker, db_session, monkeypatch):
        self._setup(client)
        csrf = self._csrf(client, "/settings")
        client.post(
            "/settings/model/extraction",
            data={"_csrf": csrf, "provider_type": "claude_cli", "model": "claude-haiku-4-5"},
        )
        csrf = self._csrf(client, "/people")
        client.post("/people", data={"_csrf": csrf, "name": "Robin Vega"})  # -> P0001

        monkeypatch.setattr(
            extraction,
            "build_provider",
            lambda cfg: FakeProvider(
                model="x",
                responder=lambda s, p: (
                    _MODEL_JSON.replace("P0007", "P0001")
                    if "2026-03-01" in p
                    else '{"facts":[],"interactions":{}}'
                ),
            ),
        )

        csrf = self._csrf(client, "/transcripts")
        r = client.post(
            "/transcripts/import",
            data={"_csrf": csrf},
            files={"file": ("chat.txt", _export(DAY1, DAY2), "text/plain")},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text[:800]
        base = r.headers["location"].split("?")[0]

        # the import guessed: "Robin Vega" -> the existing P0001 (name match),
        # "Alex" -> owner (the other side of a 1:1 with a mapped person)
        parts = {p.display_name: p for p in db_session.query(orm.SourceParticipant).all()}
        assert parts["Robin Vega"].role == "person" and parts["Robin Vega"].person_id is not None
        assert parts["Alex"].role == "owner"

        # submit the one form with the guessed mapping unchanged
        csrf = self._csrf(client, base)
        r = client.post(
            f"{base}/extract",
            data={
                "_csrf": csrf,
                f"role_{parts['Robin Vega'].id}": "person",
                f"person_{parts['Robin Vega'].id}": "P0001",
                f"role_{parts['Alex'].id}": "owner",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303 and "/extract/status" not in r.headers["location"]

        import time

        for _ in range(100):
            s = client.get(f"{base}/extract/status").json()
            if not s["active"]:
                break
            time.sleep(0.05)
        assert s["active"] is False
        assert s["processed"] >= 1 and s["total"] == s["processed"] + s["remaining"]

        page = client.get(base).text
        assert "fact(s)" in page
        assert "Coastal Studios" in client.get("/people/P0001").text
