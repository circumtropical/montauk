"""Dashboard flows for the WhatsApp connector: the disabled states, the
pair -> discover -> enable -> sync path, and disconnect keeping data."""

from __future__ import annotations

import base64
import secrets

import pytest

from montauk.connectors import service
from montauk.connectors.whatsapp import WhatsAppConnector

from ._fake_sidecar import FakeSidecar, msg

ROBIN = "15559990000@s.whatsapp.net"


@pytest.fixture
def fake(monkeypatch) -> FakeSidecar:
    f = FakeSidecar()
    monkeypatch.setattr(service, "build_connector", lambda: WhatsAppConnector(f))
    return f


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setenv("MONTAUK_WA_SIDECAR_TOKEN", "test-token")
    monkeypatch.setenv("MONTAUK_WA_SIDECAR_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("MONTAUK_MASTER_KEY", base64.b64encode(secrets.token_bytes(32)).decode())


def _setup(client):
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


def _csrf(client, path="/connectors"):
    html = client.get(path).text
    marker = 'name="_csrf" value="'
    i = html.index(marker) + len(marker)
    return html[i : html.index('"', i)]


def test_page_explains_when_sidecar_is_not_configured(client):
    _setup(client)
    html = client.get("/connectors").text
    assert "sidecar is not configured" in html or "sidecar is not running" in html
    assert "never sends" in html.lower() or "only read" in html.lower()


def test_connect_blocked_without_master_key(client, monkeypatch):
    _setup(client)
    monkeypatch.setenv("MONTAUK_WA_SIDECAR_TOKEN", "test-token")
    r = client.post("/connectors/whatsapp/connect", data={"_csrf": _csrf(client)}, follow_redirects=False)
    assert r.status_code == 303
    assert "MONTAUK_MASTER_KEY" in client.get(r.headers["location"]).text


def test_full_pair_discover_enable_sync_flow(client, fake, wired):
    _setup(client)
    # connect -> pairing
    client.post("/connectors/whatsapp/connect", data={"_csrf": _csrf(client)})
    assert client.get("/connectors/whatsapp/pair.json").json()["state"] == "pairing"

    # phone links; the poll captures identity + discovers threads
    fake.add_direct_thread(ROBIN, "Robin Vega")
    fake.complete_pairing()
    poll = client.get("/connectors/whatsapp/pair.json").json()
    assert poll["state"] == "connected" and poll["connected_as"] == "+15550001111"

    page = client.get("/connectors").text
    assert "Robin Vega" in page and "connected as +15550001111" in page

    # enable the thread, push a message, sync
    import re

    tid = re.search(r"/connectors/whatsapp/threads/([0-9a-f-]{36})", page).group(1)
    client.post(f"/connectors/whatsapp/threads/{tid}", data={"_csrf": _csrf(client), "enabled": "1"})
    fake.push_live(msg("L1", ROBIN, text="dinner Friday?"))
    client.post("/connectors/whatsapp/sync", data={"_csrf": _csrf(client)})

    # the message is now an archived transcript
    from montauk.db import models as orm

    with client.app.state.montauk.session_factory() as s:
        assert s.query(orm.SourceMessage).filter_by(provider_message_id="L1").count() == 1


def test_disconnect_keeps_archived_transcripts(client, fake, wired):
    _setup(client)
    client.post("/connectors/whatsapp/connect", data={"_csrf": _csrf(client)})
    fake.add_direct_thread(ROBIN, "Robin Vega")
    fake.complete_pairing()
    client.get("/connectors/whatsapp/pair.json")
    page = client.get("/connectors").text
    import re

    tid = re.search(r"/connectors/whatsapp/threads/([0-9a-f-]{36})", page).group(1)
    client.post(f"/connectors/whatsapp/threads/{tid}", data={"_csrf": _csrf(client), "enabled": "1"})
    fake.push_live(msg("L1", ROBIN))
    client.post("/connectors/whatsapp/sync", data={"_csrf": _csrf(client)})

    client.post("/connectors/whatsapp/disconnect", data={"_csrf": _csrf(client), "logout": "1"})
    from montauk.db import models as orm

    with client.app.state.montauk.session_factory() as s:
        assert s.query(orm.SourceMessage).filter_by(provider_message_id="L1").count() == 1
        acc = service.get_account(s, next(iter(s.query(orm.Workspace.id))).id)
        assert acc.status == "disconnected" and acc.encrypted_session is None
