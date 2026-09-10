"""Workspace-level operations for inbound connectors.

The dashboard routes and the background runner both go through here. All
functions are workspace-scoped; the async ones take a connector object
(the real :class:`WhatsAppConnector` or a fake) so they are testable
without a sidecar.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import models as orm
from ..db.crypto import SecretBox
from ..db.repositories import WorkspaceScope
from .archive import archive_messages
from .base import ConnectorError, ConnectorStatus, DiscoveredThread
from .whatsapp import PROVIDER, WhatsAppConnector, jid_to_phone

logger = logging.getLogger("montauk.connectors")

_HISTORY_PAGE = 200
_HISTORY_PAGE_BUDGET = 12  # pages per sync tick, so one thread can't hog the loop
_REDISCOVER_EVERY = dt.timedelta(minutes=2)
_last_discover: dict[uuid.UUID, dt.datetime] = {}


class ConnectorConfigError(RuntimeError):
    """The connector cannot run in this deployment (no master key, etc.)."""


# --- account lifecycle ------------------------------------------------


def get_account(
    session: Session, workspace_id: uuid.UUID, provider: str = PROVIDER
) -> orm.ConnectorAccount | None:
    return session.execute(
        select(orm.ConnectorAccount)
        .where(orm.ConnectorAccount.workspace_id == workspace_id)
        .where(orm.ConnectorAccount.provider == provider)
    ).scalar_one_or_none()


def ensure_account(scope: WorkspaceScope, *, created_by: uuid.UUID | None) -> orm.ConnectorAccount:
    account = get_account(scope.session, scope.workspace_id)
    if account is None:
        account = orm.ConnectorAccount(
            workspace_id=scope.workspace_id,
            provider=PROVIDER,
            status="unconfigured",
            created_by=created_by,
        )
        scope.session.add(account)
        scope.session.flush()
    return account


def list_live_accounts(session: Session) -> list[orm.ConnectorAccount]:
    """Accounts the runner should keep syncing -- everything except the ones
    the owner has explicitly disconnected or never configured."""
    return list(
        session.execute(
            select(orm.ConnectorAccount).where(
                orm.ConnectorAccount.status.in_(("connected", "degraded", "pairing"))
            )
        ).scalars()
    )


# --- status / session -----------------------------------------------


def _apply_status(account: orm.ConnectorAccount, st: ConnectorStatus) -> None:
    if st.state == "connected":
        account.status = "connected"
        account.last_connected_at = dt.datetime.now(dt.UTC)
        account.last_error = None
        if st.self_identity:
            account.self_identity = st.self_identity
            account.self_phone = jid_to_phone(st.self_identity)
        if st.self_display_name:
            account.self_display_name = st.self_display_name
    elif st.state == "pairing":
        account.status = "pairing"
    elif st.state in ("degraded", "disconnected"):
        account.status = st.state
    if st.last_error:
        account.last_error = st.last_error


def _persist_session(account: orm.ConnectorAccount, blob: str | None, box: SecretBox | None) -> None:
    if not blob or box is None:
        return
    try:
        current = box.decrypt(account.encrypted_session) if account.encrypted_session else None
    except Exception:  # noqa: BLE001 -- corrupt/rotated envelope: just overwrite
        current = None
    if blob != current:
        account.encrypted_session = box.encrypt(blob)
        account.session_updated_at = dt.datetime.now(dt.UTC)


def _load_session(account: orm.ConnectorAccount, box: SecretBox | None) -> str | None:
    if not account.encrypted_session or box is None:
        return None
    try:
        return box.decrypt(account.encrypted_session)
    except Exception:  # noqa: BLE001
        logger.warning("connector %s: session envelope did not decrypt", account.id)
        return None


def build_connector() -> WhatsAppConnector:
    return WhatsAppConnector()


# --- operations (async) --------------------------------------------


async def begin_pairing(
    session: Session, scope: WorkspaceScope, connector: WhatsAppConnector, *, created_by: uuid.UUID | None
) -> ConnectorStatus:
    account = ensure_account(scope, created_by=created_by)
    account.status = "pairing"
    account.last_error = None
    session.flush()
    try:
        st = await connector.start(None)
    except ConnectorError as exc:
        account.status = "unconfigured"
        account.last_error = str(exc)
        session.commit()
        raise
    _apply_status(account, st)
    session.commit()
    return st


async def refresh_status(
    session: Session,
    scope: WorkspaceScope,
    account: orm.ConnectorAccount,
    connector: WhatsAppConnector,
    box: SecretBox | None,
) -> ConnectorStatus:
    st = await connector.status()
    _apply_status(account, st)
    if st.state == "connected":
        _persist_session(account, await connector.export_session(), box)
    session.commit()
    return st


async def resume(
    session: Session,
    scope: WorkspaceScope,
    account: orm.ConnectorAccount,
    connector: WhatsAppConnector,
    box: SecretBox | None,
) -> ConnectorStatus:
    """Reconnect an account that already has a stored session."""
    blob = _load_session(account, box)
    st = await connector.start(blob)
    _apply_status(account, st)
    if st.state == "connected":
        _persist_session(account, await connector.export_session(), box)
    session.commit()
    return st


async def discover(
    session: Session, scope: WorkspaceScope, account: orm.ConnectorAccount, connector: WhatsAppConnector
) -> int:
    threads = list(await connector.discover_threads())
    existing = {
        t.provider_thread_id: t
        for t in session.execute(
            select(orm.ConnectorThread).where(orm.ConnectorThread.account_id == account.id)
        ).scalars()
    }
    for d in threads:
        _upsert_thread(session, account, d, existing.get(d.provider_thread_id))
    session.commit()
    return len(threads)


def _is_bare_identifier(title: str) -> bool:
    """A title that is really just a phone number or a raw JID -- i.e. we
    don't have a name for this conversation yet."""
    t = (title or "").strip().lstrip("+")
    return not t or t.replace(" ", "").isdigit() or "@" in t


def _upsert_thread(
    session: Session,
    account: orm.ConnectorAccount,
    d: DiscoveredThread,
    row: orm.ConnectorThread | None,
) -> orm.ConnectorThread:
    participants = [{"jid": p.identity, "name": p.display_name, "is_self": p.is_self} for p in d.participants]
    if row is None:
        row = orm.ConnectorThread(
            workspace_id=account.workspace_id,
            account_id=account.id,
            provider_thread_id=d.provider_thread_id,
            title=d.title,
            is_group=d.is_group,
            participants=participants or None,
            last_message_at=d.last_message_at,
            message_estimate=d.message_estimate,
        )
        session.add(row)
        session.flush()
        return row
    # A name that arrives later (from a pushName / address-book sync) should
    # replace a bare phone number, but not the other way around.
    if d.title and (not _is_bare_identifier(d.title) or _is_bare_identifier(row.title)):
        row.title = d.title
    row.is_group = d.is_group
    if participants:
        row.participants = participants
    if d.last_message_at and (row.last_message_at is None or d.last_message_at > row.last_message_at):
        row.last_message_at = d.last_message_at
    if d.message_estimate is not None:
        row.message_estimate = d.message_estimate
    return row


_HISTORY_BOUNDS = ("new", "days", "count", "all")


def resolve_history_bound(
    bound: str, *, days: int | None = None, count: int | None = None
) -> tuple[str, dt.datetime | None, int | None]:
    """Map a dashboard choice to (history_window, history_since, cap).

    - ``new``   -> nothing before enablement
    - ``days``  -> messages newer than N days
    - ``count`` -> the most recent N messages
    - ``all``   -> everything the linked device can reach
    """
    if bound == "new":
        return "future_only", None, None
    if bound == "days":
        n = max(1, int(days or 30))
        return "since_date", dt.datetime.now(dt.UTC) - dt.timedelta(days=n), None
    if bound == "count":
        return "all", None, max(1, int(count or 500))
    return "all", None, None


def set_thread_enabled(
    session: Session,
    scope: WorkspaceScope,
    account: orm.ConnectorAccount,
    thread_id: uuid.UUID,
    *,
    enabled: bool,
    history_window: str = "all",
    history_since: dt.datetime | None = None,
    history_message_cap: int | None = None,
) -> orm.ConnectorThread:
    row = session.execute(
        select(orm.ConnectorThread)
        .where(orm.ConnectorThread.id == thread_id)
        .where(orm.ConnectorThread.account_id == account.id)
    ).scalar_one_or_none()
    if row is None:
        raise ConnectorError("no such thread on this account")
    if enabled:
        reconfigured = (
            not row.enabled
            or row.history_window != history_window
            or row.history_since != history_since
            or row.history_message_cap != history_message_cap
        )
        row.enabled = True
        row.enabled_at = row.enabled_at or dt.datetime.now(dt.UTC)
        if reconfigured:
            row.history_window = history_window
            row.history_since = history_since
            row.history_message_cap = history_message_cap
            row.history_complete = history_window == "future_only"
            row.history_cursor = None
            row.history_synced_count = 0
    else:
        row.enabled = False
    session.commit()
    return row


async def disconnect(
    session: Session,
    scope: WorkspaceScope,
    account: orm.ConnectorAccount,
    connector: WhatsAppConnector,
    *,
    logout: bool,
) -> None:
    try:
        await connector.disconnect(logout=logout)
    except ConnectorError as exc:
        logger.warning("connector disconnect: %s", exc)
    account.status = "disconnected"
    if logout:
        account.encrypted_session = None
        account.self_identity = None
        account.self_phone = None
        account.self_display_name = None
    session.commit()


# --- sync ---------------------------------------------------------


async def sync_account(
    session: Session,
    scope: WorkspaceScope,
    account: orm.ConnectorAccount,
    connector: WhatsAppConnector,
    box: SecretBox | None,
) -> dict[str, int]:
    """One sync tick: persist session, drain live messages into enabled
    threads, and backfill enabled threads that are not history-complete."""
    st = await connector.status()
    _apply_status(account, st)
    if st.state == "connected":
        _persist_session(account, await connector.export_session(), box)
    if account.status not in ("connected", "degraded"):
        session.commit()
        return {"live": 0, "history": 0}

    # Re-discover periodically: names (pushName / address-book sync) arrive
    # asynchronously after linking, so titles improve over the first minutes.
    now = dt.datetime.now(dt.UTC)
    if now - _last_discover.get(account.id, dt.datetime.min.replace(tzinfo=dt.UTC)) > _REDISCOVER_EVERY:
        _last_discover[account.id] = now
        try:
            await discover(session, scope, account, connector)
        except ConnectorError:
            pass

    enabled = {
        t.provider_thread_id: t
        for t in session.execute(
            select(orm.ConnectorThread)
            .where(orm.ConnectorThread.account_id == account.id)
            .where(orm.ConnectorThread.enabled.is_(True))
        ).scalars()
    }

    live = 0
    drained = await connector.drain(after=account.inbox_seq)
    buckets: dict[str, list] = {}
    for m in drained.messages:
        buckets.setdefault(m.provider_thread_id, []).append(m)
    for jid, msgs in buckets.items():
        ct = enabled.get(jid)
        if ct is None:
            continue  # selection gate: never archive an unenabled thread
        live += archive_messages(session, scope, account, ct, msgs).new_messages
    if drained.seq > account.inbox_seq:
        account.inbox_seq = drained.seq
    if drained.gap:
        for ct in enabled.values():
            ct.history_complete = False

    history = 0
    for ct in enabled.values():
        if not ct.history_complete:
            history += await _backfill_thread(session, scope, account, ct, connector)

    account.last_sync_at = dt.datetime.now(dt.UTC)
    session.commit()
    return {"live": live, "history": history}


async def _backfill_thread(
    session: Session,
    scope: WorkspaceScope,
    account: orm.ConnectorAccount,
    ct: orm.ConnectorThread,
    connector: WhatsAppConnector,
) -> int:
    if ct.history_window == "future_only":
        ct.history_complete = True
        return 0
    floor = ct.history_since if ct.history_window == "since_date" else None
    cap = ct.history_message_cap
    added = 0
    for _ in range(_HISTORY_PAGE_BUDGET):
        remaining = None if cap is None else max(0, cap - ct.history_synced_count)
        if remaining == 0:
            ct.history_complete = True
            break
        limit = _HISTORY_PAGE if remaining is None else min(_HISTORY_PAGE, remaining)
        page = await connector.fetch_history(ct.provider_thread_id, cursor=ct.history_cursor, limit=limit)
        batch = [m for m in page.messages if floor is None or m.sent_at >= floor]
        if batch:
            added += archive_messages(session, scope, account, ct, batch).new_messages
            ct.history_synced_count += len(batch)
        ct.history_cursor = page.next_cursor
        hit_floor = floor is not None and any(m.sent_at < floor for m in page.messages)
        hit_cap = cap is not None and ct.history_synced_count >= cap
        if page.done or hit_floor or hit_cap or page.next_cursor is None:
            ct.history_complete = True
            break
        session.flush()
    return added


# --- views for the dashboard / MCP --------------------------------


@dataclass
class ConnectorThreadView:
    id: uuid.UUID
    title: str
    phone: str | None
    has_name: bool
    is_group: bool
    participant_names: list[str]
    last_message_at: dt.datetime | None
    message_estimate: int | None
    enabled: bool
    history_window: str
    history_message_cap: int | None
    history_complete: bool
    archived_messages: int
    source_thread_id: uuid.UUID | None

    @property
    def search_key(self) -> str:
        return f"{self.title} {self.phone or ''} {' '.join(self.participant_names)}".lower()

    @property
    def history_label(self) -> str:
        if self.history_window == "future_only":
            return "new messages only"
        if self.history_window == "since_date":
            return "recent history"
        if self.history_message_cap:
            return f"last {self.history_message_cap} messages"
        return "all history"


@dataclass
class ConnectorAccountView:
    provider: str
    status: str
    self_display_name: str | None
    self_phone: str | None
    last_connected_at: dt.datetime | None
    last_sync_at: dt.datetime | None
    last_error: str | None
    has_session: bool
    threads: list[ConnectorThreadView]

    @property
    def enabled_threads(self) -> int:
        return sum(1 for t in self.threads if t.enabled)

    @property
    def monitored(self) -> list[ConnectorThreadView]:
        return [t for t in self.threads if t.enabled]

    @property
    def available(self) -> list[ConnectorThreadView]:
        return [t for t in self.threads if not t.enabled]

    @property
    def is_live(self) -> bool:
        return self.status in ("connected", "degraded")


def account_view(session: Session, workspace_id: uuid.UUID) -> ConnectorAccountView | None:
    account = get_account(session, workspace_id)
    if account is None:
        return None
    rows = list(
        session.execute(
            select(orm.ConnectorThread)
            .where(orm.ConnectorThread.account_id == account.id)
            .order_by(orm.ConnectorThread.last_message_at.desc().nullslast())
        ).scalars()
    )
    counts: dict[uuid.UUID, int] = {
        tid: n
        for tid, n in session.execute(
            select(orm.SourceMessage.thread_id, func.count(orm.SourceMessage.id))
            .where(orm.SourceMessage.connector_account_id == account.id)
            .group_by(orm.SourceMessage.thread_id)
        ).all()
    }
    threads = [
        ConnectorThreadView(
            id=r.id,
            title=r.title,
            phone=jid_to_phone(r.provider_thread_id),
            has_name=not _is_bare_identifier(r.title),
            is_group=r.is_group,
            participant_names=[
                p.get("name") or p.get("jid") for p in (r.participants or []) if not p.get("is_self")
            ],
            last_message_at=r.last_message_at,
            message_estimate=r.message_estimate,
            enabled=r.enabled,
            history_window=r.history_window,
            history_message_cap=r.history_message_cap,
            history_complete=r.history_complete,
            archived_messages=int(counts.get(r.source_thread_id, 0)) if r.source_thread_id else 0,
            source_thread_id=r.source_thread_id,
        )
        for r in rows
    ]
    return ConnectorAccountView(
        provider=account.provider,
        status=account.status,
        self_display_name=account.self_display_name,
        self_phone=account.self_phone,
        last_connected_at=account.last_connected_at,
        last_sync_at=account.last_sync_at,
        last_error=account.last_error,
        has_session=bool(account.encrypted_session),
        threads=threads,
    )


def health(session: Session, workspace_id: uuid.UUID) -> dict[str, object]:
    """Connector health for the agent API -- no secrets, no message content."""
    view = account_view(session, workspace_id)
    if view is None:
        return {"provider": PROVIDER, "status": "unconfigured", "enabled_threads": 0}
    return {
        "provider": view.provider,
        "status": view.status,
        "connected_as": view.self_phone,
        "enabled_threads": view.enabled_threads,
        "discovered_threads": len(view.threads),
        "last_sync_at": view.last_sync_at.isoformat() if view.last_sync_at else None,
        "last_error": view.last_error,
    }
