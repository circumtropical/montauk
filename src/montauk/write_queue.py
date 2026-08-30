"""Serialized write queue (spec section 5, 23, Appendix B).

An in-process `asyncio.Lock` is correct and sufficient for the supported
deployment topology: one long-running `montauk` process per data_dir,
serving either one local stdio agent or many remote HTTP agents. As
cheap defense-in-depth in case that assumption is ever violated (e.g. a
user misconfiguring two separate stdio instances against the same
data_dir), the actual write also takes an OS-level advisory lock
(`fcntl.flock`) underneath the asyncio lock.

Every mutation tool goes through `WriteQueue.submit()`, which enforces
"all writes serialized" (spec section 5) by construction: callers pass a
plain synchronous callable that does the load-validate-write-reindex
work for exactly one person.
"""

from __future__ import annotations

import asyncio
import fcntl
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")

LOCK_FILENAME = ".montauk.lock"


class WriteQueue:
    def __init__(self, data_dir: Path | str):
        self.data_dir = Path(data_dir)
        self._lock_path = self.data_dir / LOCK_FILENAME
        self._asyncio_lock = asyncio.Lock()

    async def submit(self, operation: Callable[[], T]) -> T:
        """Run `operation` with exclusive access, serialized against every
        other call to `submit` on this queue. `operation` must be a plain
        synchronous callable (all of Montauk's file/SQLite IO is
        synchronous); it runs in a worker thread so it never blocks the
        event loop while other requests wait their turn.
        """
        async with self._asyncio_lock:
            return await asyncio.to_thread(self._run_with_flock, operation)

    def _run_with_flock(self, operation: Callable[[], T]) -> T:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with open(self._lock_path, "w") as fd:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
            try:
                return operation()
            finally:
                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
