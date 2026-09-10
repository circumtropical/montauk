# ADR 0004 — WhatsApp inbound connector

Date: 2026-09-10
Status: Accepted (vertical slice); contract-suite hardening + Docker packaging tracked as follow-ups

Spec Appendix C decision #1 (Baileys vs whatsmeow) and §11 (WhatsApp connector).
This ADR also records the connector-framework choices in §10 that the WhatsApp
build forces.

## 1. Linked-device library: **Baileys** (Node/TypeScript sidecar)

**Decision:** implement WhatsApp as a linked device using **Baileys** in a small
Node sidecar (`sidecars/whatsapp/`), wrapped in a receive-only adapter.

Rationale, against the spec's stated criteria:

| Criterion | Baileys | whatsmeow |
| --- | --- | --- |
| Maintenance status | Active, large contributor base, frequent protocol fixes | Active, fewer maintainers, tracks the same protocol |
| Protocol compatibility | Good; regressions are usually fixed within days | Good; mautrix-whatsapp depends on it |
| Session durability | Adequate with a single-blob auth store (see §3) | Very good |
| History sync | Exposes `messaging-history.set` + on-demand `fetchMessageHistory` | Similar |
| Container complexity | **Node 24 already runs on the target VPS — no new toolchain** | Needs a Go build/runtime added to the box |
| Resource use | ~90–130 MB RSS idle | ~30–50 MB idle |
| Testability | JS; easy to stub the socket and unit-test the adapter | Go; requires a Go test harness alongside the Python suite |

The deciding factors are **toolchain** (Node is already present and maintained on
the deployment; Go is not) and **testability alongside the existing Python
suite**. whatsmeow's lower memory footprint and session robustness are real but
do not outweigh adding a second language runtime to a single-operator
deployment. Revisit if session drops become frequent in practice.

A full time-boxed spike against a live account was not run — pairing needs a
real phone and the comparison above is from the libraries' documentation and
source. The product contract is unchanged by the choice.

## 2. Read-only enforcement

- The sidecar builds the Baileys socket and subscribes to events only. It
  never calls `sendMessage`, `sendReceipt`, `readMessages`, `chatModify`,
  `sendPresenceUpdate`, `updateProfileStatus`, `groupParticipantsUpdate`, or
  any other mutating method. `sidecars/whatsapp/server.js` is scanned by a test
  (`tests/phase2/test_connector_readonly.py`) that fails if any appears.
- `montauk.connectors.base.InboundConnector` is a `Protocol` with exactly
  `start`, `discover_threads`, `fetch_history`, `drain`, `status`, `disconnect`.
  `assert_inbound_only(obj)` rejects any attribute whose name matches a
  send/mutate verb; a static test runs it against `WhatsAppConnector`.
- The dashboard and agent API call it an **inbound connector** and state that
  Montauk never sends.
- `POST /logout` on the sidecar unlinks *our own device* (the §10 disconnect
  operation). It changes no conversation and is the only Baileys call that
  writes anything to WhatsApp's servers.

## 3. Session custody: **Montauk-custodied**

The linked-device credentials live as one JSON blob
(`JSON.stringify({creds, keys}, BufferJSON.replacer)`), encrypted with
`SecretBox` (AES-256-GCM, `MONTAUK_MASTER_KEY`) in
`connector_accounts.encrypted_session`. The sidecar holds credentials only in
memory: Montauk passes the blob to `POST /connect`, and the sidecar reports a
`session_epoch` that bumps on every `creds.update`. The Montauk driver polls,
and whenever the epoch advances it fetches `GET /session` and re-encrypts.
The sidecar keeps **no** auth state on disk, so a workspace backup captures the
session and the sidecar container/host is stateless.

Trade-off: a `creds.update` that happens while the Montauk driver is down and is
followed by a sidecar restart is lost, forcing a re-pair. Acceptable for a
single-operator deployment; the driver polls every 10 s while connected and
always fetches the session on graceful shutdown.

## 4. Transport: sidecar is an HTTP server on loopback; Montauk is the client

Montauk → sidecar over `127.0.0.1`, `Authorization: Bearer $MONTAUK_WA_SIDECAR_TOKEN`.
All flow is Montauk-initiated (`/connect`, `/status`, `/threads`, `/history`,
`/inbox?after=`, `/session`, `/disconnect`, `/logout`). The sidecar keeps a
bounded in-memory ring of live messages tagged with a monotonic `seq`; Montauk
archives them and acknowledges by passing the highest `seq` it stored. A ring
overflow (Montauk was down too long) sets `gap: true`, and the driver responds
with a history backfill of the affected threads. This makes both processes
independently restartable and idempotent (spec §10): dedup is by provider
message ID, so re-delivery after a crash is harmless.

## 5. Storage: shared source archive, connector rows for selection/health

- `connector_accounts` — one row per (workspace, provider); status state
  machine `unconfigured → pairing → connected → degraded → disconnected`;
  encrypted session; self identity; last sync / last error.
- `connector_threads` — every discovered conversation with `enabled` and a
  history window. **No message content is stored until `enabled`** (spec §10.2).
- Enabled threads archive into the existing `source_threads` /
  `source_messages` / `source_participants` tables (`platform = "whatsapp"`,
  distinct from the importer's `"whatsapp_import"`), so the transcript browser,
  extraction, and dedup work unchanged. `source_messages` gains
  `provider_message_id` (dedup key 1), `direction`, `sender_identity`, and
  `content_omitted`; `source_threads` gains `connector_account_id` and
  `provider_thread_id`.
- Cross-source dedup between a manual export and the connected account of the
  *same* chat is **not** attempted in this slice (different thread identity, no
  IDs in exports). Per spec §15 the bias is to keep a possible duplicate. A
  later pass can reconcile an import thread onto a connector thread.

## 6. Identity mapping: connector suggests, human confirms

`source_participants` gains `suggested_person_id`. The connector never sets
`role`/`person_id` — it records a suggestion (exact contact-method match on the
sender's phone, else an unambiguous name match) and leaves `role = "unmapped"`.
Extraction still requires `role = "person"` with a confirmed `person_id`, which
only the owner can set on the transcript page (spec §14). Connectors and
extraction jobs never create a person.

## 7. Process model: a background asyncio driver in the dashboard

No separate worker process yet (consistent with ADR 0002 §5). The dashboard
process runs one `asyncio` task (`connectors.runner.run`) that drives every
connected account: load session → connect the sidecar → drain live messages →
backfill enabled threads that are not `history_complete` → persist session on
epoch change → update account status. A single-flight guard keeps the MCP
process from also running it. The sidecar itself is a separate
`montauk-whatsapp` systemd `--user` service.

## 8. Attachments

Never downloaded. A media message is archived as a text row with
`content_omitted = true` and any caption as the text (spec §9.1).
