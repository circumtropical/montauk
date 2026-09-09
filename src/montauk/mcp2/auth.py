"""Bearer-token authentication for the Phase 2 MCP transport.

A coarse connection-level gate (``BearerAuthMiddleware``) rejects any
request without a valid, non-revoked agent credential before it reaches
MCP dispatch. Per-tool capability checks (``memory_read`` /
``memory_write``) run inside each tool via ``session_scope``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from ..services.agent_credentials import authenticate


def extract_bearer_token(headers: dict[str, str] | None) -> str | None:
    """Pull the token out of an ``Authorization: Bearer <token>`` header
    (case-insensitive header name and scheme). Returns None when absent or
    malformed."""
    if not headers:
        return None
    value = next((v for k, v in headers.items() if k.lower() == "authorization"), None)
    if not value or not value.lower().startswith("bearer "):
        return None
    return value[len("bearer ") :].strip() or None


class BearerAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, *, session_factory: sessionmaker[Session]) -> None:
        super().__init__(app)
        self._session_factory = session_factory

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        token = extract_bearer_token(dict(request.headers))
        if not token:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        with self._session_factory() as session:
            cred = authenticate(session, token)
            if cred is not None:
                session.commit()
        if cred is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)
