# Serving the dashboard and the MCP server on one hostname

The Phase 2 dashboard and the Phase 1 MCP server are two separate processes on
two loopback ports. They can share a single public hostname by routing on the
URL path in the reverse proxy — the MCP transport lives entirely under `/mcp`,
and the dashboard uses every other path.

| URL | Serves | Loopback port |
| --- | --- | --- |
| `https://montauk.example.com/mcp` | MCP streamable-HTTP transport (agents) | 8765 |
| `https://montauk.example.com/` (everything else) | Web dashboard (browser) | 8817 |

## Caddy

Replace the existing `montauk.example.com { ... }` block with:

```caddy
montauk.example.com {
	encode zstd gzip

	# Agents connect here; keep the bearer-token auth, no basicauth.
	handle /mcp* {
		reverse_proxy 127.0.0.1:8765 {
			header_up X-Real-IP {remote_host}
		}
	}

	# Everything else is the dashboard (its own password/session auth + CSP).
	handle {
		reverse_proxy 127.0.0.1:8817 {
			header_up X-Real-IP {remote_host}
		}
	}

	log {
		output file /var/log/caddy/montauk.access.log
	}
}
```

Then `sudo systemctl reload caddy`.

## Dashboard process

Run it with `--behind-proxy` so it trusts `X-Forwarded-*` from localhost and
marks session cookies `Secure`:

```ini
# ~/.config/systemd/user/montauk-dashboard.service
ExecStart=/path/to/.venv/bin/montauk dashboard --host 127.0.0.1 --port 8817 --behind-proxy
```

(or set `MONTAUK_DASHBOARD_SECURE_COOKIES=1` in the environment file).

Nothing about the MCP server changes. `transport.public_url` stays
`https://montauk.example.com`; the transport already accepts that forwarded
Host header.

### If you use the `claude` / `codex` CLI LLM provider

The dashboard shells out to the CLI, so the service must be able to find and
run it:

```ini
[Service]
# ~/.local/bin (claude) + the node bin if the CLI shells out to node
Environment=PATH=/home/you/.local/bin:/home/you/.nvm/versions/node/<ver>/bin:/usr/local/bin:/usr/bin:/bin
# the CLI writes session state under its config dir, so keep it writable
# even with ProtectHome=read-only
ReadWritePaths=... -/home/you/.claude -/home/you/.codex -/home/you/.cache
```

`which claude` from your shell tells you the right bin dir; the CLI must
already be signed in (`claude` interactively once) — that's the explicit
owner action, no API key involved. Alternatively put the full path in the
dashboard's "CLI binary" field.

## Alternative: a separate port

If you would rather keep paths clean, give the dashboard its own listener:
`montauk-dashboard.example.com { reverse_proxy 127.0.0.1:8817 }` (a second
Caddy site block, its own certificate, automatic). Path routing avoids the
extra DNS record and certificate; a subdomain avoids any chance of a path
collision if the dashboard ever adds a top-level `/mcp…` route (it will not).
