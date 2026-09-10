"""The provider-neutral inbound connector interface (spec 10).

A connector exposes exactly six operations -- ``start``, ``discover_threads``,
``fetch_history``, ``drain``, ``status``, ``disconnect``. None of them can
mutate a remote conversation. :func:`assert_inbound_only` rejects any object
that carries a method whose name looks like a send/mutate verb, and a static
test runs it against every connector implementation.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# Verbs that would make a connector capable of writing to the remote service.
# Matched against method/attribute names (case-insensitive, word-ish).
_FORBIDDEN_VERBS = (
    "send",
    "reply",
    "react",
    "post",
    "publish",
    "write",
    "delete",
    "remove",
    "edit",
    "update",
    "revoke",
    "markread",
    "mark_read",
    "read_receipt",
    "receipt",
    "presence",
    "typing",
    "compose",
    "archive_remote",
    "mute",
    "block",
    "unblock",
    "leave",
    "join",
    "invite",
    "kick",
)
_ALLOWED = {"start", "discover_threads", "fetch_history", "drain", "status", "disconnect"}


class ConnectorError(RuntimeError):
    """A connector operation failed in a way the caller should surface."""


class OutboundCapabilityError(AssertionError):
    """A connector object exposed a method that could write to the remote
    service. This is a programming error, not a runtime condition."""


@dataclass(frozen=True)
class ThreadParticipant:
    identity: str  # provider id (WhatsApp JID)
    display_name: str
    is_self: bool = False


@dataclass(frozen=True)
class DiscoveredThread:
    provider_thread_id: str
    title: str
    is_group: bool
    participants: tuple[ThreadParticipant, ...] = ()
    last_message_at: dt.datetime | None = None
    message_estimate: int | None = None


@dataclass(frozen=True)
class IncomingMessage:
    provider_message_id: str
    provider_thread_id: str
    sender_identity: str
    sender_display_name: str
    from_me: bool
    sent_at: dt.datetime
    text: str
    kind: str = "text"  # text | media | system | revoked
    attachment_type: str | None = None
    seq: int | None = None  # live-buffer sequence (drain only)

    @property
    def content_omitted(self) -> bool:
        return self.kind == "media"

    @property
    def is_system(self) -> bool:
        return self.kind == "system"

    @property
    def direction(self) -> str:
        return "outbound" if self.from_me else "inbound"


@dataclass(frozen=True)
class HistoryPage:
    messages: tuple[IncomingMessage, ...]
    next_cursor: dict | None
    done: bool


@dataclass
class ConnectorStatus:
    state: str  # unconfigured | pairing | connected | degraded | disconnected
    self_identity: str | None = None
    self_display_name: str | None = None
    self_phone: str | None = None
    pairing_code: str | None = None  # QR payload to render while pairing
    pairing_expires_at: dt.datetime | None = None
    last_error: str | None = None
    session_epoch: int = 0
    session_blob: str | None = None  # opaque; persisted encrypted by the caller


@dataclass
class DrainResult:
    messages: tuple[IncomingMessage, ...] = ()
    seq: int = 0  # highest seq represented; ack by passing it back as `after`
    gap: bool = False  # live buffer overflowed -- caller must backfill history
    extras: dict = field(default_factory=dict)


@runtime_checkable
class InboundConnector(Protocol):
    provider: str

    async def start(self, session_blob: str | None) -> ConnectorStatus:
        """Load a stored session (or begin pairing) and connect."""

    async def status(self) -> ConnectorStatus: ...

    async def discover_threads(self) -> Sequence[DiscoveredThread]: ...

    async def fetch_history(
        self, provider_thread_id: str, *, cursor: dict | None, limit: int
    ) -> HistoryPage: ...

    async def drain(self, *, after: int) -> DrainResult:
        """Return live messages buffered since sequence ``after``."""

    async def disconnect(self, *, logout: bool = False) -> ConnectorStatus:
        """Stop syncing. ``logout`` also unlinks this device (spec 10)."""


def _looks_outbound(name: str) -> bool:
    if name.startswith("_") or name in _ALLOWED:
        return False
    flat = re.sub(r"[^a-z]", "", name.lower())
    return any(verb.replace("_", "") in flat for verb in _FORBIDDEN_VERBS)


def assert_inbound_only(obj: object) -> None:
    """Raise :class:`OutboundCapabilityError` if ``obj`` exposes any public
    attribute whose name suggests it can write to the remote service. Used
    as a guard in tests for every connector implementation."""
    offenders = sorted(n for n in dir(obj) if _looks_outbound(n))
    if offenders:
        raise OutboundCapabilityError(
            f"{type(obj).__name__} exposes possible outbound method(s): {', '.join(offenders)}"
        )
