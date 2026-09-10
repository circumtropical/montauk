"""WhatsApp inbound connector (spec 10, 11): pairing, discovery, the
selection gate, idempotent archiving, and identity suggestions."""

from __future__ import annotations

import datetime as dt

import pytest

from montauk.connectors import service
from montauk.connectors.whatsapp import WhatsAppConnector, jid_to_phone
from montauk.db import models as orm
from montauk.db.repositories import PeopleRepository
from montauk.models import ContactInfo, Person

from ._fake_sidecar import FakeSidecar, msg

ROBIN = "15559990000@s.whatsapp.net"


@pytest.fixture
def fake() -> FakeSidecar:
    return FakeSidecar()


@pytest.fixture
def connector(fake: FakeSidecar) -> WhatsAppConnector:
    return WhatsAppConnector(fake)


async def _connect(db_session, scope, connector, fake, secret_box):
    await service.begin_pairing(db_session, scope, connector, created_by=None)
    fake.complete_pairing()
    account = service.get_account(db_session, scope.workspace_id)
    await service.refresh_status(db_session, scope, account, connector, secret_box)
    return account


def _enable_first_thread(db_session, scope, account, window="all"):
    ct = db_session.query(orm.ConnectorThread).filter_by(account_id=account.id).first()
    service.set_thread_enabled(db_session, scope, account, ct.id, enabled=True, history_window=window)
    return ct


class TestPairing:
    async def test_pairing_then_connected_captures_identity_and_session(
        self, db_session, scope, connector, fake, secret_box
    ):
        st = await service.begin_pairing(db_session, scope, connector, created_by=None)
        assert st.state == "pairing" and st.pairing_code
        account = service.get_account(db_session, scope.workspace_id)
        assert account.status == "pairing"

        fake.complete_pairing()
        await service.refresh_status(db_session, scope, account, connector, secret_box)
        assert account.status == "connected"
        assert account.self_phone == "+15550001111"
        assert account.encrypted_session and secret_box.decrypt(account.encrypted_session)

    async def test_resume_uses_the_stored_session(self, db_session, scope, connector, fake, secret_box):
        account = await _connect(db_session, scope, connector, fake, secret_box)
        stored = secret_box.decrypt(account.encrypted_session)
        fake.state = "disconnected"

        await service.resume(db_session, scope, account, connector, secret_box)
        assert fake.session_blob == stored
        assert account.status == "connected"


class TestDiscovery:
    async def test_discover_upserts_threads_without_archiving(
        self, db_session, scope, connector, fake, secret_box
    ):
        fake.add_direct_thread(ROBIN, "Robin Vega")
        fake.add_direct_thread("family@g.us", "Family")
        account = await _connect(db_session, scope, connector, fake, secret_box)

        n = await service.discover(db_session, scope, account, connector)
        assert n == 2
        rows = db_session.query(orm.ConnectorThread).filter_by(account_id=account.id).all()
        assert {r.title for r in rows} == {"Robin Vega", "Family"}
        assert all(not r.enabled for r in rows)
        # selection gate: nothing archived yet
        assert db_session.query(orm.SourceMessage).count() == 0


class TestSelectionGate:
    async def test_live_message_for_an_unenabled_thread_is_dropped(
        self, db_session, scope, connector, fake, secret_box
    ):
        fake.add_direct_thread(ROBIN, "Robin Vega")
        account = await _connect(db_session, scope, connector, fake, secret_box)
        await service.discover(db_session, scope, account, connector)

        fake.push_live(msg("m1", ROBIN, text="unenabled, should be dropped"))
        await service.sync_account(db_session, scope, account, connector, secret_box)

        assert db_session.query(orm.SourceMessage).count() == 0
        # ...but the live cursor still advances so we don't re-scan it forever
        assert account.inbox_seq == 1


class TestArchiving:
    async def _ready(self, db_session, scope, connector, fake, secret_box):
        fake.add_direct_thread(ROBIN, "Robin Vega")
        account = await _connect(db_session, scope, connector, fake, secret_box)
        await service.discover(db_session, scope, account, connector)
        ct = _enable_first_thread(db_session, scope, account)
        return account, ct

    async def test_history_and_live_land_in_the_source_archive(
        self, db_session, scope, connector, fake, secret_box
    ):
        account, ct = await self._ready(db_session, scope, connector, fake, secret_box)
        base = dt.datetime(2026, 2, 1, 9, 0, tzinfo=dt.UTC)
        fake.history[ROBIN] = [
            msg("h1", ROBIN, text="older one", at=base),
            msg("h2", ROBIN, text="reply", from_me=True, at=base + dt.timedelta(minutes=1)),
        ]
        fake.push_live(msg("L1", ROBIN, text="live now", at=base + dt.timedelta(days=10)))

        counts = await service.sync_account(db_session, scope, account, connector, secret_box)
        assert counts["live"] == 1 and counts["history"] == 2

        db_session.refresh(ct)
        assert ct.source_thread_id is not None and ct.history_complete
        msgs = db_session.query(orm.SourceMessage).order_by(orm.SourceMessage.sent_at).all()
        assert [m.provider_message_id for m in msgs] == ["h1", "h2", "L1"]
        assert {m.direction for m in msgs} == {"inbound", "outbound"}
        assert all(m.connector_account_id == account.id for m in msgs)

    async def test_provider_id_dedup_makes_resync_idempotent(
        self, db_session, scope, connector, fake, secret_box
    ):
        account, ct = await self._ready(db_session, scope, connector, fake, secret_box)
        fake.history[ROBIN] = [msg("h1", ROBIN), msg("h2", ROBIN, text="two")]
        await service.sync_account(db_session, scope, account, connector, secret_box)
        n1 = db_session.query(orm.SourceMessage).count()

        ct.history_complete = False  # force another history pass
        db_session.commit()
        await service.sync_account(db_session, scope, account, connector, secret_box)
        assert db_session.query(orm.SourceMessage).count() == n1 == 2

    async def test_owner_side_is_marked_owner_and_recipient_is_a_suggestion_only(
        self, db_session, scope, connector, fake, secret_box
    ):
        PeopleRepository(scope).create(
            Person(id="P0007", name="Robin Vega", contact=ContactInfo(phones=["+15559990000"]))
        )
        db_session.flush()
        account, ct = await self._ready(db_session, scope, connector, fake, secret_box)
        fake.push_live(msg("L1", ROBIN, text="hey", sender_name="Robin"))
        fake.push_live(msg("L2", ROBIN, text="hi back", from_me=True))
        await service.sync_account(db_session, scope, account, connector, secret_box)

        parts = {p.display_name: p for p in db_session.query(orm.SourceParticipant).all()}
        owner = next(p for p in parts.values() if p.role == "owner")
        assert owner.source_identity == account.self_identity
        robin = next(p for p in parts.values() if p.role != "owner")
        assert robin.role == "unmapped"  # a connector never confirms a mapping
        assert robin.suggested_person_id is not None  # matched on the phone contact method

        # the transcript page surfaces that as an unconfirmed suggestion
        from montauk.services import transcripts

        db_session.commit()
        view = transcripts.get_thread(db_session, scope, ct.source_thread_id)
        pv = next(p for p in view.participants if p.role == "unmapped")
        assert pv.suggested_public_id == "P0007" and view.effective_mapped == 1

    async def test_media_is_a_placeholder_never_a_download(
        self, db_session, scope, connector, fake, secret_box
    ):
        account, ct = await self._ready(db_session, scope, connector, fake, secret_box)
        fake.push_live(msg("L1", ROBIN, text="", kind="media"))
        await service.sync_account(db_session, scope, account, connector, secret_box)
        m = db_session.query(orm.SourceMessage).one()
        assert m.content_omitted and m.media_omitted
        assert m.processing_status == "skipped"


class TestDisconnect:
    async def test_logout_clears_session_but_keeps_transcripts(
        self, db_session, scope, connector, fake, secret_box
    ):
        fake.add_direct_thread(ROBIN, "Robin Vega")
        account = await _connect(db_session, scope, connector, fake, secret_box)
        await service.discover(db_session, scope, account, connector)
        _enable_first_thread(db_session, scope, account)
        fake.push_live(msg("L1", ROBIN))
        await service.sync_account(db_session, scope, account, connector, secret_box)
        assert db_session.query(orm.SourceMessage).count() == 1

        await service.disconnect(db_session, scope, account, connector, logout=True)
        assert account.status == "disconnected"
        assert account.encrypted_session is None and account.self_identity is None
        assert db_session.query(orm.SourceMessage).count() == 1  # kept


class TestHealth:
    async def test_health_has_no_secrets(self, db_session, scope, connector, fake, secret_box):
        fake.add_direct_thread(ROBIN, "Robin Vega")
        account = await _connect(db_session, scope, connector, fake, secret_box)
        await service.discover(db_session, scope, account, connector)
        _enable_first_thread(db_session, scope, account)
        db_session.commit()

        h = service.health(db_session, scope.workspace_id)
        assert h == {
            "provider": "whatsapp",
            "status": "connected",
            "connected_as": "+15550001111",
            "enabled_threads": 1,
            "discovered_threads": 1,
            "last_sync_at": None,
            "last_error": None,
        }
        assert "encrypted_session" not in h and "session" not in str(h)


def test_jid_to_phone():
    assert jid_to_phone("15551234567@s.whatsapp.net") == "+15551234567"
    assert jid_to_phone("15551234567:12@s.whatsapp.net") == "+15551234567"
    assert jid_to_phone("family@g.us") is None
    assert jid_to_phone(None) is None
