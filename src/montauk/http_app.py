"""Streamable-HTTP transport wiring (spec section 30): wraps the MCP
server's ASGI app with bearer-token authentication so an unauthenticated
server is never exposed on the network.

This is a coarse connection-level gate: it rejects any request without
a valid, non-revoked credential before it reaches MCP protocol dispatch
at all. Fine-grained read_only/read_write enforcement still happens per
tool call via _authorize_write (tools_core.py), reading the same
Authorization header through Context.headers.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .auth import CredentialStore, extract_bearer_token


class BearerAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, credential_store: CredentialStore) -> None:
        super().__init__(app)
        self.credential_store = credential_store

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        token = extract_bearer_token(dict(request.headers))
        identity = self.credential_store.verify_token(token) if token else None
        if identity is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def build_http_app(server: MCPServer, *, credential_store: CredentialStore | None, host: str) -> Starlette:
    app = server.streamable_http_app(host=host)
    if credential_store is not None:
        app.add_middleware(BearerAuthMiddleware, credential_store=credential_store)
    return app
