# Serving the dashboard and the MCP server on one hostname

`montauk dashboard` and `montauk mcp` are two separate processes on two loopback
ports. They can share a single public hostname by routing on the URL path in the
reverse proxy — the MCP transport lives entirely under `/mcp`, and the dashboard
uses every other path.

| URL | Serves | Loopback port |
| --- | --- | --- |
| `https://montauk.example.com/mcp` | MCP streamable-HTTP transport (agents) | 8766 |
| `https://montauk.example.com/` (everything else) | Web dashboard (browser) | 8817 |

## Caddy

Replace the existing `montauk.example.com { ... }` block with:

```caddy
montauk.example.com {
	encode zstd gzip

	# Agents connect here; keep the bearer-token auth, no basicauth.
	handle /mcp* {
		reverse_proxy 127.0.0.1:8766 {
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

The MCP server (`montauk mcp`) needs nothing special for the proxy: it runs with
the Host/Origin allowlist off (the bearer-token gate is what secures it), so it
accepts the forwarded Host header as-is.

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

## WhatsApp connector (optional)

The WhatsApp inbound connector needs a third process — a receive-only Baileys
sidecar (`sidecars/whatsapp/`). It is **not** exposed through the reverse proxy;
the dashboard talks to it on loopback.

```
cd ~/montauk/sidecars/whatsapp && npm install
printf 'MONTAUK_WA_SIDECAR_TOKEN=%s\n' "$(openssl rand -hex 32)" > ~/montauk-data/whatsapp-sidecar.env
chmod 600 ~/montauk-data/whatsapp-sidecar.env

cp ~/montauk/deploy/montauk-whatsapp.service ~/.config/systemd/user/
#  ... edit the node path to `which node` ...
systemctl --user daemon-reload && systemctl --user enable --now montauk-whatsapp
```

Then add to `~/montauk-data/dashboard.env` (the dashboard drives the sidecar and
stores the encrypted linked-device session):

```
MONTAUK_WA_SIDECAR_URL=http://127.0.0.1:8766
MONTAUK_WA_SIDECAR_TOKEN=<same token as whatsapp-sidecar.env>
```

`MONTAUK_MASTER_KEY` must be set (the session is stored encrypted). Restart the
dashboard, then pair from **Connectors → Connect WhatsApp**. The MCP process
should keep `MONTAUK_RUN_CONNECTORS=0` (or leave the sidecar vars unset in its
env) so only the dashboard runs the sync loop. See
`docs/adr/0004-whatsapp-inbound-connector.md`.

## Alternative: a separate port

If you would rather keep paths clean, give the dashboard its own listener:
`montauk-dashboard.example.com { reverse_proxy 127.0.0.1:8817 }` (a second
Caddy site block, its own certificate, automatic). Path routing avoids the
extra DNS record and certificate; a subdomain avoids any chance of a path
collision if the dashboard ever adds a top-level `/mcp…` route (it will not).
