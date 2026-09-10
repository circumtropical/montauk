"""A scriptable stand-in for the WhatsApp Baileys sidecar.

Satisfies the same shape ``WhatsAppConnector`` delegates to, so connector
service / archive / contract tests run with no Node process.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

from montauk.connectors.base import (
    ConnectorStatus,
    DiscoveredThread,
    DrainResult,
    HistoryPage,
    IncomingMessage,
    ThreadParticipant,
)

_SELF = {"jid": "15550001111@s.whatsapp.net", "name": "Me", "phone": "+15550001111"}


def msg(
    provider_id: str,
    jid: str,
    *,
    text: str = "hi",
    from_me: bool = False,
    sender_name: str = "Robin",
    sender_jid: str | None = None,
    at: dt.datetime | None = None,
    kind: str = "text",
) -> IncomingMessage:
    return IncomingMessage(
        provider_message_id=provider_id,
        provider_thread_id=jid,
        sender_identity=sender_jid or (_SELF["jid"] if from_me else jid),
        sender_display_name="" if from_me else sender_name,
        from_me=from_me,
        sent_at=at or dt.datetime(2026, 3, 1, 12, 0, tzinfo=dt.UTC),
        text=text,
        kind=kind,
    )


class FakeSidecar:
    provider = "whatsapp"

    def __init__(self) -> None:
        self.state = "unconfigured"
        self.qr: str | None = None
        self.epoch = 1
        self.session_blob: str | None = None
        self.threads: list[DiscoveredThread] = []
        self.history: dict[str, list[IncomingMessage]] = {}
        self.live: list[IncomingMessage] = []
        self._seq = 0
        self.gap = False
        self.last_error: str | None = None
        self.calls: list[str] = []

    # -- test controls --
    def complete_pairing(self) -> None:
        self.state = "connected"
        self.qr = None

    def add_direct_thread(self, jid: str, name: str) -> DiscoveredThread:
        t = DiscoveredThread(
            provider_thread_id=jid,
            title=name,
            is_group=False,
            participants=(
                ThreadParticipant(identity=jid, display_name=name),
                ThreadParticipant(identity=_SELF["jid"], display_name="Me", is_self=True),
            ),
            last_message_at=dt.datetime(2026, 3, 1, 12, 0, tzinfo=dt.UTC),
        )
        self.threads.append(t)
        return t

    def push_live(self, m: IncomingMessage) -> None:
        self._seq += 1
        self.live.append(replace(m, seq=self._seq))

    def drop_live_buffer(self) -> None:
        self.live.clear()
        self.gap = True

    # -- sidecar protocol --
    async def start(self, session_blob: str | None) -> ConnectorStatus:
        self.calls.append("start")
        if session_blob:
            self.session_blob = session_blob
            self.state = "connected"
        else:
            self.state = "pairing"
            self.qr = "data:image/png;base64,QQ=="
        return await self.status()

    async def status(self) -> ConnectorStatus:
        self.calls.append("status")
        live = self.state == "connected"
        return ConnectorStatus(
            state=self.state,
            self_identity=_SELF["jid"] if live else None,
            self_display_name=_SELF["name"] if live else None,
            self_phone=_SELF["phone"] if live else None,
            pairing_code=self.qr,
            last_error=self.last_error,
            session_epoch=self.epoch,
        )

    async def export_session(self) -> str | None:
        self.calls.append("export_session")
        self.session_blob = self.session_blob or f"session-blob-{self.epoch}"
        return self.session_blob

    async def discover_threads(self) -> list[DiscoveredThread]:
        self.calls.append("discover_threads")
        return list(self.threads)

    async def fetch_history(self, provider_thread_id: str, *, cursor: dict | None, limit: int) -> HistoryPage:
        self.calls.append("fetch_history")
        msgs = sorted(self.history.get(provider_thread_id, []), key=lambda m: m.sent_at)
        before = cursor.get("before_ts") if cursor else None
        eligible = [m for m in msgs if before is None or m.sent_at.timestamp() < before]
        page = eligible[-limit:] if limit < len(eligible) else eligible
        oldest = page[0].sent_at.timestamp() if page else before
        return HistoryPage(
            messages=tuple(page),
            next_cursor={"before_ts": oldest} if len(page) >= limit else None,
            done=len(page) < limit,
        )

    async def drain(self, *, after: int) -> DrainResult:
        self.calls.append("drain")
        items = tuple(m for m in self.live if (m.seq or 0) > after)
        gap, self.gap = self.gap, False
        return DrainResult(
            messages=items,
            seq=self.live[-1].seq if self.live else after,
            gap=gap,
        )

    async def disconnect(self, *, logout: bool = False) -> ConnectorStatus:
        self.calls.append("disconnect")
        self.state = "disconnected"
        if logout:
            self.session_blob = None
        return await self.status()
