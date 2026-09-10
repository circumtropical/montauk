"""Spec 32.8 connector contract cases, run against the WhatsApp adapter
with a fake sidecar. Every connector implementation must pass this suite.
"""

from __future__ import annotations

import datetime as dt

import pytest

from montauk.connectors import service
from montauk.connectors.whatsapp import WhatsAppConnector
from montauk.db import models as orm

from ._fake_sidecar import FakeSidecar, msg

JID = "15559990000@s.whatsapp.net"


@pytest.fixture
def fake() -> FakeSidecar:
    return FakeSidecar()


@pytest.fixture
def connector(fake: FakeSidecar) -> WhatsAppConnector:
    return WhatsAppConnector(fake)


async def _connected_account(db_session, scope, connector, fake, secret_box):
    await service.begin_pairing(db_session, scope, connector, created_by=None)
    fake.complete_pairing()
    account = service.get_account(db_session, scope.workspace_id)
    await service.refresh_status(db_session, scope, account, connector, secret_box)
    fake.add_direct_thread(JID, "Robin")
    await service.discover(db_session, scope, account, connector)
    return account


def _enable(db_session, scope, account, **kw):
    ct = db_session.query(orm.ConnectorThread).filter_by(account_id=account.id).first()
    service.set_thread_enabled(db_session, scope, account, ct.id, enabled=True, **kw)
    return ct


class TestContract:
    async def test_capability_setup_state_machine(self, db_session, scope, connector, fake, secret_box):
        acc = service.ensure_account(scope, created_by=None)
        assert acc.status == "unconfigured"
        await service.begin_pairing(db_session, scope, connector, created_by=None)
        assert acc.status == "pairing"
        fake.complete_pairing()
        await service.refresh_status(db_session, scope, acc, connector, secret_box)
        assert acc.status == "connected"
        await service.disconnect(db_session, scope, acc, connector, logout=False)
        assert acc.status == "disconnected"

    async def test_initial_and_incremental_sync(self, db_session, scope, connector, fake, secret_box):
        acc = await _connected_account(db_session, scope, connector, fake, secret_box)
        _enable(db_session, scope, acc)
        fake.history[JID] = [msg("h1", JID, at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC))]
        assert (await service.sync_account(db_session, scope, acc, connector, secret_box))["history"] == 1
        fake.push_live(msg("L1", JID, text="new"))
        assert (await service.sync_account(db_session, scope, acc, connector, secret_box))["live"] == 1
        assert db_session.query(orm.SourceMessage).count() == 2

    async def test_both_message_directions(self, db_session, scope, connector, fake, secret_box):
        acc = await _connected_account(db_session, scope, connector, fake, secret_box)
        _enable(db_session, scope, acc)
        fake.push_live(msg("in", JID, text="you there?"))
        fake.push_live(msg("out", JID, text="yes", from_me=True))
        await service.sync_account(db_session, scope, acc, connector, secret_box)
        dirs = {m.provider_message_id: m.direction for m in db_session.query(orm.SourceMessage)}
        assert dirs == {"in": "inbound", "out": "outbound"}

    async def test_thread_selection_gate(self, db_session, scope, connector, fake, secret_box):
        acc = await _connected_account(db_session, scope, connector, fake, secret_box)
        fake.push_live(msg("L1", JID))
        await service.sync_account(db_session, scope, acc, connector, secret_box)
        assert db_session.query(orm.SourceMessage).count() == 0

    async def test_historical_boundary_since_date(self, db_session, scope, connector, fake, secret_box):
        acc = await _connected_account(db_session, scope, connector, fake, secret_box)
        _enable(db_session, scope, acc, history_window="since_date")
        ct = db_session.query(orm.ConnectorThread).filter_by(account_id=acc.id).first()
        ct.history_since = dt.datetime(2026, 2, 15, tzinfo=dt.UTC)
        db_session.commit()
        fake.history[JID] = [
            msg("old", JID, at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC)),
            msg("new", JID, at=dt.datetime(2026, 3, 1, tzinfo=dt.UTC)),
        ]
        await service.sync_account(db_session, scope, acc, connector, secret_box)
        ids = {m.provider_message_id for m in db_session.query(orm.SourceMessage)}
        assert ids == {"new"}

    async def test_historical_future_only_stores_no_history(
        self, db_session, scope, connector, fake, secret_box
    ):
        acc = await _connected_account(db_session, scope, connector, fake, secret_box)
        _enable(db_session, scope, acc, history_window="future_only")
        fake.history[JID] = [msg("old", JID, at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC))]
        await service.sync_account(db_session, scope, acc, connector, secret_box)
        assert db_session.query(orm.SourceMessage).count() == 0

    async def test_pagination_cursor_replay(self, db_session, scope, connector, fake, secret_box):
        acc = await _connected_account(db_session, scope, connector, fake, secret_box)
        _enable(db_session, scope, acc)
        fake.history[JID] = [
            msg(f"h{i}", JID, at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC) + dt.timedelta(hours=i))
            for i in range(450)
        ]
        await service.sync_account(db_session, scope, acc, connector, secret_box)
        assert db_session.query(orm.SourceMessage).count() == 450  # paged 200+200+50

    async def test_provider_id_dedupe_across_history_and_live(
        self, db_session, scope, connector, fake, secret_box
    ):
        acc = await _connected_account(db_session, scope, connector, fake, secret_box)
        _enable(db_session, scope, acc)
        same = msg("dup", JID, text="counts once")
        fake.history[JID] = [same]
        fake.push_live(same)
        await service.sync_account(db_session, scope, acc, connector, secret_box)
        assert db_session.query(orm.SourceMessage).filter_by(provider_message_id="dup").count() == 1

    async def test_live_buffer_gap_forces_history_backfill(
        self, db_session, scope, connector, fake, secret_box
    ):
        acc = await _connected_account(db_session, scope, connector, fake, secret_box)
        ct = _enable(db_session, scope, acc)
        fake.history[JID] = [msg("h1", JID)]
        await service.sync_account(db_session, scope, acc, connector, secret_box)
        db_session.refresh(ct)
        assert ct.history_complete
        # a dropped live buffer -> next sync re-scans history
        fake.history[JID].append(msg("h2", JID, text="missed while down"))
        fake.drop_live_buffer()
        await service.sync_account(db_session, scope, acc, connector, secret_box)
        assert db_session.query(orm.SourceMessage).filter_by(provider_message_id="h2").count() == 1

    async def test_expired_session_surfaces_as_status_not_a_crash(
        self, db_session, scope, connector, fake, secret_box
    ):
        acc = await _connected_account(db_session, scope, connector, fake, secret_box)
        fake.state = "degraded"
        fake.last_error = "connection replaced"
        await service.refresh_status(db_session, scope, acc, connector, secret_box)
        assert acc.status == "degraded" and acc.last_error == "connection replaced"

    async def test_restart_resumes_from_stored_session(self, db_session, scope, connector, fake, secret_box):
        acc = await _connected_account(db_session, scope, connector, fake, secret_box)
        blob = secret_box.decrypt(acc.encrypted_session)
        # simulate a process restart: a brand-new connector + sidecar that
        # starts disconnected, and a runner-style resume.
        fake2 = FakeSidecar()
        conn2 = WhatsAppConnector(fake2)
        await service.resume(db_session, scope, acc, conn2, secret_box)
        assert fake2.session_blob == blob and acc.status == "connected"

    async def test_no_attachment_is_ever_persisted(self, db_session, scope, connector, fake, secret_box):
        acc = await _connected_account(db_session, scope, connector, fake, secret_box)
        _enable(db_session, scope, acc)
        fake.push_live(msg("v1", JID, text="", kind="media"))
        await service.sync_account(db_session, scope, acc, connector, secret_box)
        m = db_session.query(orm.SourceMessage).one()
        assert m.content_omitted and m.text in ("", "[media omitted]")

    def test_adapter_exposes_only_the_six_inbound_ops(self):
        from montauk.connectors.base import assert_inbound_only

        assert_inbound_only(connector := WhatsAppConnector(FakeSidecar()))
        public = {n for n in dir(connector) if not n.startswith("_")}
        assert public == {
            "provider",
            "start",
            "status",
            "export_session",
            "discover_threads",
            "fetch_history",
            "drain",
            "disconnect",
        }
