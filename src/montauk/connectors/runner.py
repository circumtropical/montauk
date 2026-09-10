"""Background driver for inbound connectors.

One asyncio task, started in the dashboard's lifespan. Each tick it walks
every connector account that should be live, reconnects it through the
sidecar if needed, and runs a sync. The MCP process does not start this
(guarded by ``MONTAUK_RUN_CONNECTORS`` + a configured sidecar).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from ..db import models as orm
from ..db.repositories import Actor, WorkspaceScope
from . import service
from .sidecar import sidecar_configured

logger = logging.getLogger("montauk.connectors.runner")

ENV_ENABLE = "MONTAUK_RUN_CONNECTORS"
_DEFAULT_INTERVAL = 10.0
_RESUME_COOLDOWN = 60.0  # never re-issue /connect for the same account faster than this
_last_resume: dict[str, float] = {}


def runner_enabled() -> bool:
    return sidecar_configured() and os.environ.get(ENV_ENABLE, "1") != "0"


async def run(state: object, *, interval: float = _DEFAULT_INTERVAL, iterations: int | None = None) -> None:
    n = 0
    while iterations is None or n < iterations:
        n += 1
        try:
            await tick(state)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- a background loop must not die
            logger.exception("connector runner tick failed")
        if iterations is not None and n >= iterations:
            return
        await asyncio.sleep(interval)


async def tick(state: object) -> None:
    factory = state.session_factory  # type: ignore[attr-defined]
    box = state.secret_box  # type: ignore[attr-defined]
    with factory() as session:
        targets = [(a.id, a.workspace_id) for a in service.list_live_accounts(session) if a.encrypted_session]

    for account_id, workspace_id in targets:
        with factory() as session:
            account = session.get(orm.ConnectorAccount, account_id)
            if account is None:
                continue
            scope = WorkspaceScope(session, workspace_id, Actor("system", "connector-runner"))
            connector = service.build_connector()
            try:
                st = await connector.status()
                # Only re-issue /connect when the sidecar has genuinely lost
                # the session -- never during a "connecting" handshake, and at
                # most once a minute, or we churn the socket and WhatsApp
                # times out its init queries.
                key = str(account_id)
                if st.state in ("disconnected", "unconfigured", "degraded"):
                    if time.monotonic() - _last_resume.get(key, 0.0) > _RESUME_COOLDOWN:
                        _last_resume[key] = time.monotonic()
                        await service.resume(session, scope, account, connector, box)
                if account.status in ("connected", "degraded"):
                    await service.sync_account(session, scope, account, connector, box)
                session.commit()
            except Exception:  # noqa: BLE001
                logger.exception("sync failed for connector account %s", account_id)
                session.rollback()
