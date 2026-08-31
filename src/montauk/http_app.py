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
from urllib.parse import urlsplit

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
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


def build_transport_security(
    *, mode: str, host: str, port: int, public_url: str | None
) -> TransportSecuritySettings | None:
    """DNS-rebinding protection settings for the streamable-HTTP transport.

    Only relevant for mode="remote" (stdio never builds an HTTP app).
    Returning None lets the MCP SDK apply its own default, which for a
    loopback bind host is a localhost-only Host-header allowlist -- that
    rejects the real hostname a reverse proxy forwards, forcing an
    awkward `header_up Host 127.0.0.1:PORT` rewrite in the proxy config.

    Setting transport.public_url adds that hostname to the allowlist so
    the proxy needs no Host rewrite. Without it, we disable the check for
    remote transports: every request is already gated by
    BearerAuthMiddleware, and DNS-rebinding attacks target browsers
    reaching localhost dev servers, not authenticated server-to-server
    APIs behind TLS.
    """
    if mode != "remote":
        return None

    allowed_hosts = [
        f"{host}:{port}",
        f"127.0.0.1:{port}",
        f"localhost:{port}",
        f"[::1]:{port}",
    ]
    allowed_origins: list[str] = []

    if public_url:
        parts = urlsplit(public_url)
        hostname = parts.hostname or ""
        if ":" in hostname:  # bracket IPv6 literals to match Host-header form
            hostname = f"[{hostname}]"
        scheme = parts.scheme
        url_port = parts.port or (443 if scheme == "https" else 80)
        allowed_hosts += [hostname, f"{hostname}:{url_port}"]
        allowed_origins += [f"{scheme}://{hostname}", f"{scheme}://{hostname}:{url_port}"]

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(public_url),
        allowed_hosts=list(dict.fromkeys(allowed_hosts)),
        allowed_origins=list(dict.fromkeys(allowed_origins)),
    )


def build_http_app(
    server: MCPServer,
    *,
    credential_store: CredentialStore | None,
    host: str,
    transport_security: TransportSecuritySettings | None = None,
) -> Starlette:
    app = server.streamable_http_app(host=host, transport_security=transport_security)
    if credential_store is not None:
        app.add_middleware(BearerAuthMiddleware, credential_store=credential_store)
    return app
