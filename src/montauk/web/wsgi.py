"""ASGI entrypoint for `uvicorn montauk.web.wsgi:app`.

Reads the DSN from ``MONTAUK_DATABASE_URL`` / ``DATABASE_URL`` and whether
to require secure cookies from ``MONTAUK_DASHBOARD_SECURE_COOKIES``
(default: secure). ``montauk dashboard`` sets both.
"""

from __future__ import annotations

import os

from .app import create_app

app = create_app(
    secure_cookies=os.environ.get("MONTAUK_DASHBOARD_SECURE_COOKIES", "1") != "0",
)
