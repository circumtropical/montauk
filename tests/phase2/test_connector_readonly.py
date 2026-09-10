"""Spec 10.1: prove the connector abstraction and the WhatsApp sidecar
expose no way to send / reply / react / mark-read / delete / edit a remote
message."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from montauk.connectors.base import (
    OutboundCapabilityError,
    assert_inbound_only,
)
from montauk.connectors.sidecar import SidecarClient
from montauk.connectors.whatsapp import WhatsAppConnector

from ._fake_sidecar import FakeSidecar

_SIDECAR_JS = Path(__file__).resolve().parents[2] / "sidecars" / "whatsapp" / "server.js"

# Baileys methods that would write to WhatsApp's servers. `logout` is
# deliberately absent -- it is the connector "disconnect" operation (spec 10).
_FORBIDDEN_BAILEYS_CALLS = [
    "sendMessage",
    "sendReceipt",
    "sendReceipts",
    "readMessages",
    "chatModify",
    "sendPresenceUpdate",
    "presenceSubscribe",
    "updateProfileStatus",
    "updateProfileName",
    "updateProfilePicture",
    "removeProfilePicture",
    "updateBlockStatus",
    "groupCreate",
    "groupParticipantsUpdate",
    "groupUpdateSubject",
    "groupUpdateDescription",
    "groupToggleEphemeral",
    "groupSettingUpdate",
    "groupLeave",
    "sendMessageAck",
]


@pytest.mark.parametrize("connector", [WhatsAppConnector(FakeSidecar()), SidecarClient()])
def test_connector_exposes_no_outbound_method(connector):
    assert_inbound_only(connector)


def test_assert_inbound_only_actually_catches_a_sender():
    class Bad:
        async def send_message(self, *a):  # noqa: D401
            ...

    with pytest.raises(OutboundCapabilityError):
        assert_inbound_only(Bad())


@pytest.mark.skipif(not _SIDECAR_JS.exists(), reason="sidecar source not present")
def test_sidecar_source_calls_no_write_method():
    src = _SIDECAR_JS.read_text("utf-8")
    # strip line comments so the header's mention of the calls doesn't trip it
    code = "\n".join(re.sub(r"//.*$", "", line) for line in src.splitlines())
    offenders = [name for name in _FORBIDDEN_BAILEYS_CALLS if re.search(rf"\.\s*{name}\s*\(", code)]
    assert not offenders, f"sidecar calls forbidden Baileys method(s): {offenders}"


@pytest.mark.skipif(not _SIDECAR_JS.exists(), reason="sidecar source not present")
def test_sidecar_disables_presence_broadcast():
    src = _SIDECAR_JS.read_text("utf-8")
    assert "markOnlineOnConnect: false" in src
