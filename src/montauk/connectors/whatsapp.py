"""The WhatsApp inbound connector (spec 11).

All protocol work happens in the Baileys sidecar; this class is the typed,
inbound-only seam the rest of Montauk talks to. It delegates every call to
a sidecar-shaped object (:class:`~montauk.connectors.sidecar.SidecarClient`
in production, a fake in tests) and adds only WhatsApp-specific helpers.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Protocol

from .base import ConnectorStatus, DiscoveredThread, DrainResult, HistoryPage
from .sidecar import SidecarClient

PROVIDER = "whatsapp"
_JID_USER_RE = re.compile(r"^(\d+)(?:[:.]\d+)?@")


def jid_to_phone(jid: str | None) -> str | None:
    """``15551234567@s.whatsapp.net`` -> ``+15551234567``. Groups and other
    non-numeric JIDs return ``None``."""
    if not jid:
        return None
    m = _JID_USER_RE.match(jid)
    if not m:
        return None
    return "+" + m.group(1)


class _Sidecar(Protocol):
    async def start(self, session_blob: str | None) -> ConnectorStatus: ...
    async def status(self) -> ConnectorStatus: ...
    async def export_session(self) -> str | None: ...
    async def discover_threads(self) -> Sequence[DiscoveredThread]: ...
    async def fetch_history(
        self, provider_thread_id: str, *, cursor: dict | None, limit: int
    ) -> HistoryPage: ...
    async def drain(self, *, after: int) -> DrainResult: ...
    async def disconnect(self, *, logout: bool = False) -> ConnectorStatus: ...


class WhatsAppConnector:
    provider = PROVIDER

    def __init__(self, sidecar: _Sidecar | None = None):
        self._sidecar: _Sidecar = sidecar or SidecarClient()

    async def start(self, session_blob: str | None) -> ConnectorStatus:
        return await self._sidecar.start(session_blob)

    async def status(self) -> ConnectorStatus:
        return await self._sidecar.status()

    async def export_session(self) -> str | None:
        return await self._sidecar.export_session()

    async def discover_threads(self) -> Sequence[DiscoveredThread]:
        return await self._sidecar.discover_threads()

    async def fetch_history(self, provider_thread_id: str, *, cursor: dict | None, limit: int) -> HistoryPage:
        return await self._sidecar.fetch_history(provider_thread_id, cursor=cursor, limit=limit)

    async def drain(self, *, after: int) -> DrainResult:
        return await self._sidecar.drain(after=after)

    async def disconnect(self, *, logout: bool = False) -> ConnectorStatus:
        return await self._sidecar.disconnect(logout=logout)
