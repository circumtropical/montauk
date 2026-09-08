"""Construct the Phase 2 MCP server and its streamable-HTTP app."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from .auth import BearerAuthMiddleware
from .context import Mcp2Context
from .instructions import SERVER_INSTRUCTIONS
from .tools import register_mcp2_tools


def create_mcp2_server(ctx: Mcp2Context, *, name: str = "montauk") -> MCPServer:
    server = MCPServer(name=name, instructions=SERVER_INSTRUCTIONS)
    register_mcp2_tools(server, ctx)
    return server


def build_mcp2_app(
    ctx: Mcp2Context,
    *,
    host: str = "127.0.0.1",
    transport_security: TransportSecuritySettings | None = None,
    require_auth: bool = True,
) -> Starlette:
    server = create_mcp2_server(ctx)
    # Explicitly disable the SDK's Host/Origin allowlist. Left as None, the
    # SDK enables it with a localhost-only allowlist whenever host is
    # loopback (server.py), which 421s every request a reverse proxy
    # forwards with the real Host. The bearer-token gate -- not a Host
    # allowlist -- is what keeps this server closed; DNS-rebinding attacks
    # target browsers reaching localhost dev servers, not an authenticated
    # server-to-server API behind TLS.
    if transport_security is None:
        transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    app = server.streamable_http_app(host=host, transport_security=transport_security)
    if require_auth:
        app.add_middleware(BearerAuthMiddleware, session_factory=ctx.session_factory)
    return app
