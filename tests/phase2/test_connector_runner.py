"""The background connector runner: it is off unless a sidecar is
configured, and a tick drives a live account's sync."""

from __future__ import annotations

from montauk.connectors import runner, service
from montauk.connectors.whatsapp import WhatsAppConnector
from montauk.db import models as orm

from ._fake_sidecar import FakeSidecar, msg

ROBIN = "15559990000@s.whatsapp.net"


def test_runner_disabled_without_a_sidecar(monkeypatch):
    monkeypatch.delenv("MONTAUK_WA_SIDECAR_TOKEN", raising=False)
    assert runner.runner_enabled() is False


def test_runner_disabled_by_env_flag(monkeypatch):
    monkeypatch.setenv("MONTAUK_WA_SIDECAR_TOKEN", "t")
    monkeypatch.setenv("MONTAUK_RUN_CONNECTORS", "0")
    assert runner.runner_enabled() is False


async def test_tick_syncs_a_live_account(session_maker, db_session, scope, secret_box, monkeypatch):
    fake = FakeSidecar()
    monkeypatch.setattr(service, "build_connector", lambda: WhatsAppConnector(fake))

    conn = WhatsAppConnector(fake)
    await service.begin_pairing(db_session, scope, conn, created_by=None)
    fake.complete_pairing()
    account = service.get_account(db_session, scope.workspace_id)
    await service.refresh_status(db_session, scope, account, conn, secret_box)
    fake.add_direct_thread(ROBIN, "Robin")
    await service.discover(db_session, scope, account, conn)
    ct = db_session.query(orm.ConnectorThread).filter_by(account_id=account.id).first()
    service.set_thread_enabled(db_session, scope, account, ct.id, enabled=True)
    fake.push_live(msg("L1", ROBIN, text="from the runner"))
    db_session.commit()

    class _State:
        session_factory = session_maker
        secret_box = None

    _State.secret_box = secret_box
    await runner.tick(_State())

    with session_maker() as s:
        assert s.query(orm.SourceMessage).filter_by(provider_message_id="L1").count() == 1


async def test_tick_resumes_a_disconnected_sidecar(session_maker, db_session, scope, secret_box, monkeypatch):
    fake = FakeSidecar()
    monkeypatch.setattr(service, "build_connector", lambda: WhatsAppConnector(fake))
    conn = WhatsAppConnector(fake)
    await service.begin_pairing(db_session, scope, conn, created_by=None)
    fake.complete_pairing()
    account = service.get_account(db_session, scope.workspace_id)
    await service.refresh_status(db_session, scope, account, conn, secret_box)
    db_session.commit()
    blob = secret_box.decrypt(account.encrypted_session)

    fake.state = "disconnected"  # sidecar restarted, lost its socket

    class _State:
        session_factory = session_maker

    _State.secret_box = secret_box
    await runner.tick(_State())
    assert fake.session_blob == blob
    assert "start" in fake.calls
