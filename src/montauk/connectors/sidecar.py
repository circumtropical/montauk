"""HTTP client for the WhatsApp Baileys sidecar (``sidecars/whatsapp/``).

Montauk is always the client; the sidecar is a loopback HTTP server. Only
the calls needed for an inbound connector are implemented here -- there is
deliberately no ``send``/``reply``/``react`` method to call.
"""

from __future__ import annotations

import datetime as dt
import os
from typing import Any

import httpx

from .base import (
    ConnectorError,
    ConnectorStatus,
    DiscoveredThread,
    DrainResult,
    HistoryPage,
    IncomingMessage,
    ThreadParticipant,
)

ENV_SIDECAR_URL = "MONTAUK_WA_SIDECAR_URL"
ENV_SIDECAR_TOKEN = "MONTAUK_WA_SIDECAR_TOKEN"
_DEFAULT_URL = "http://127.0.0.1:8766"


def sidecar_configured() -> bool:
    return bool(os.environ.get(ENV_SIDECAR_TOKEN))


def _ts(value: Any) -> dt.datetime | None:
    if value in (None, "", 0):
        return None
    if isinstance(value, int | float):
        return dt.datetime.fromtimestamp(float(value), tz=dt.UTC)
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _message(raw: dict[str, Any]) -> IncomingMessage:
    return IncomingMessage(
        provider_message_id=str(raw["provider_id"]),
        provider_thread_id=str(raw["jid"]),
        sender_identity=str(raw.get("sender_jid") or raw["jid"]),
        sender_display_name=str(raw.get("sender_name") or ""),
        from_me=bool(raw.get("from_me")),
        sent_at=_ts(raw.get("timestamp")) or dt.datetime.now(dt.UTC),
        text=str(raw.get("text") or ""),
        kind=str(raw.get("kind") or "text"),
        attachment_type=raw.get("attachment_type"),
        seq=raw.get("seq"),
    )


def _status(raw: dict[str, Any]) -> ConnectorStatus:
    self_ = raw.get("self") or {}
    return ConnectorStatus(
        state=str(raw.get("state") or "disconnected"),
        self_identity=self_.get("jid"),
        self_display_name=self_.get("name"),
        self_phone=self_.get("phone"),
        pairing_code=raw.get("qr"),
        pairing_expires_at=_ts(raw.get("qr_expires_at")),
        last_error=raw.get("last_error"),
        session_epoch=int(raw.get("session_epoch") or 0),
    )


class SidecarClient:
    """Thin async wrapper over the sidecar's loopback API."""

    provider = "whatsapp"

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ):
        self._base = (base_url or os.environ.get(ENV_SIDECAR_URL) or _DEFAULT_URL).rstrip("/")
        self._token = token or os.environ.get(ENV_SIDECAR_TOKEN) or ""
        self._transport = transport
        self._timeout = timeout

    async def _call(self, method: str, path: str, *, json: Any = None) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            async with httpx.AsyncClient(
                base_url=self._base, timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.request(method, path, json=json, headers=headers)
        except httpx.HTTPError as exc:
            raise ConnectorError(f"WhatsApp sidecar unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise ConnectorError(f"WhatsApp sidecar {path} -> {resp.status_code}: {resp.text[:200]}")
        return resp.json() if resp.content else {}

    async def start(self, session_blob: str | None) -> ConnectorStatus:
        raw = await self._call("POST", "/connect", json={"session": session_blob})
        return _status(raw)

    async def status(self) -> ConnectorStatus:
        return _status(await self._call("GET", "/status"))

    async def export_session(self) -> str | None:
        raw = await self._call("GET", "/session")
        blob = raw.get("session")
        return blob if isinstance(blob, str) else None

    async def discover_threads(self) -> list[DiscoveredThread]:
        raw = await self._call("GET", "/threads")
        out: list[DiscoveredThread] = []
        for t in raw.get("threads", []):
            out.append(
                DiscoveredThread(
                    provider_thread_id=str(t["jid"]),
                    title=str(t.get("name") or t["jid"]),
                    is_group=bool(t.get("is_group")),
                    participants=tuple(
                        ThreadParticipant(
                            identity=str(p["jid"]),
                            display_name=str(p.get("name") or ""),
                            is_self=bool(p.get("is_me")),
                        )
                        for p in t.get("participants", [])
                    ),
                    last_message_at=_ts(t.get("last_ts")),
                    message_estimate=t.get("count_estimate"),
                )
            )
        return out

    async def fetch_history(self, provider_thread_id: str, *, cursor: dict | None, limit: int) -> HistoryPage:
        raw = await self._call(
            "POST",
            "/history",
            json={"jid": provider_thread_id, "before_cursor": cursor, "limit": limit},
        )
        return HistoryPage(
            messages=tuple(_message(m) for m in raw.get("messages", [])),
            next_cursor=raw.get("next_cursor"),
            done=bool(raw.get("done")),
        )

    async def drain(self, *, after: int) -> DrainResult:
        raw = await self._call("GET", f"/inbox?after={after}")
        return DrainResult(
            messages=tuple(_message(m) for m in raw.get("messages", [])),
            seq=int(raw.get("seq") or after),
            gap=bool(raw.get("gap")),
        )

    async def disconnect(self, *, logout: bool = False) -> ConnectorStatus:
        raw = await self._call("POST", "/logout" if logout else "/disconnect")
        return _status(raw or {"state": "disconnected"})
