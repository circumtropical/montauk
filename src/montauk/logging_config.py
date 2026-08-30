"""Ordinary rotating application logs (spec section 28).

Deliberately minimal by default: handlers here never log fact text,
full person records, contact details, or raw request payloads -- tool
handlers don't pass that content to the logger at all (see
tools_core.py/tools_ops.py), so there is nothing to redact here. Audit
lines (tool name, agent, status, latency) carry only identifiers.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from .config import LoggingConfig

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(config: LoggingConfig, *, log_dir: Path | str | None = None) -> None:
    root = logging.getLogger("montauk")
    root.setLevel(config.level.upper())
    root.handlers.clear()

    formatter = logging.Formatter(_FORMAT)

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    if log_dir is not None:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = TimedRotatingFileHandler(
            log_dir / "montauk.log",
            when="midnight",
            backupCount=config.retention_days,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    root.propagate = False
