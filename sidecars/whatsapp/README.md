# Montauk WhatsApp sidecar

A **receive-only** WhatsApp linked-device bridge (Baileys) for the Montauk
inbound connector. See `docs/adr/0004-whatsapp-inbound-connector.md`.

Montauk talks to this over loopback HTTP and **never sends** through it. The
only call that writes to WhatsApp's servers is `POST /logout` (unlink this
device), which is the connector "disconnect" operation. As a linked device,
Baileys still acknowledges message *delivery* at the protocol level — that is
unavoidable for any linked device and marks nothing as read for you. Montauk
issues no read receipts and broadcasts no presence (`markOnlineOnConnect: false`).

## Run

```
cd sidecars/whatsapp
npm install
MONTAUK_WA_SIDECAR_TOKEN=$(openssl rand -hex 32) \
MONTAUK_WA_SIDECAR_PORT=8766 \
npm start
```

Then in the Montauk dashboard/MCP environment set:

```
MONTAUK_WA_SIDECAR_URL=http://127.0.0.1:8766
MONTAUK_WA_SIDECAR_TOKEN=<the same token>
```

Pair from the dashboard: **Connectors → Connect WhatsApp**, scan the QR with
*WhatsApp → Settings → Linked devices → Link a device*.

## API (all require `Authorization: Bearer $MONTAUK_WA_SIDECAR_TOKEN`)

| Method | Path | Purpose |
| --- | --- | --- |
| GET  | `/status` | connection state, self identity, current QR (data URL), `session_epoch` |
| POST | `/connect` | `{session}` — load a stored session blob (or `null` to pair) and connect |
| GET  | `/session` | the current linked-device credentials blob (Montauk stores it encrypted) |
| GET  | `/threads` | discovered conversations (metadata only) |
| POST | `/history` | `{jid, before_cursor, limit}` — page backward through buffered history |
| GET  | `/inbox?after=<seq>` | live messages buffered since `seq`; `gap:true` if the buffer overflowed |
| POST | `/disconnect` | close the socket, keep the session |
| POST | `/logout` | unlink this device and clear the session |

## Session custody

The sidecar keeps credentials **in memory only**. Montauk passes the blob on
`/connect` and re-fetches it via `/session` whenever `session_epoch` advances,
storing it encrypted (`MONTAUK_MASTER_KEY`). Restarting the sidecar loses
nothing as long as Montauk is up to re-`/connect` it.

## History depth

The initial link syncs whatever history WhatsApp pushes (`syncFullHistory:
true`) — typically several months. Deeper on-demand backfill
(`fetchMessageHistory`) is a follow-up; the manual chat-export importer covers
older history in the meantime.
