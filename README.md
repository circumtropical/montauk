# Montauk

Relationship memory for personal agents: durable, private, structured memory of the people
in one person's life, with a web dashboard for the owner and an MCP server for their agent.

The canonical store is **PostgreSQL**, workspace-scoped, with field-level revision history.
An agent reaches it over an authenticated MCP endpoint; the owner curates it through a
server-rendered web dashboard. `montauk_phase2_build_and_migration_spec.md` and `docs/adr/`
are the design record.

> Montauk began as a Markdown-file store with a stdio MCP server (`montauk_phase1_build_spec.md`).
> That store and its server were removed once the PostgreSQL store, the migrator that imported
> it, and this MCP surface were all live. The retrieval engine (`person_context.py`) and the
> local semantic index (`semantic_index.py`, currently unused) carried over.

**Identity vs. name.** Each person has a permanent, generic ID (`P0001`, `P0002`, ...) that
Montauk assigns and never changes or reuses. The person's `name` is just the best label
currently known -- it can be partial or wrong at first and corrected later with
`update_person_name` (former spellings are kept as searchable aliases), without touching the ID.

**Briefing-first retrieval.** The normal agent call is `prepare_person_briefing(person_id,
purpose, detail_level, mode)`: it selects the relevant curated evidence, compresses it with a
configured low-cost model into a short, factual, purpose-specific briefing, and returns that
plus lightweight `source_refs`. The agent drills into individual records with
`get_context_sources` / `get_facts` / `get_interactions` only to verify a claim or get detail
the briefing left out. A narrow factual question ("what is X's birthday?") is answered from
the structured field with no model call. `prepare_person_context` is the deterministic,
no-LLM path -- ranked raw records, hybrid BM25 + (optional) semantic retrieval, no synthesis.
Montauk never generates advice, quotations, or missing facts; that is the agent's job.

## Running it

```bash
uv sync
export MONTAUK_DATABASE_URL=postgresql://user:pass@localhost:5432/montauk
export MONTAUK_MASTER_KEY=$(python -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())")

uv run montauk db upgrade      # create / update the schema
uv run montauk dashboard       # http://127.0.0.1:8817  (first visit = setup wizard)
uv run montauk mcp             # http://127.0.0.1:8766/mcp  (agent endpoint)
```

- `MONTAUK_MASTER_KEY` (32 bytes, base64) encrypts stored model-provider API keys. Without it,
  briefings fall back to the deterministic evidence packet.
- Both servers refuse to start against a stale schema; run `montauk db upgrade` after a pull.
- Put a TLS-terminating reverse proxy in front for anything beyond loopback. One hostname can
  serve both: route `/mcp*` to the MCP port, everything else to the dashboard
  (`docs/deploy-phase2-shared-host.md`).

## Agent access

Create a bearer token for the agent in the dashboard (**Settings -> agent tokens**), then point
an MCP-compatible host at the streamable-HTTP endpoint with `Authorization: Bearer <token>`.
Every request is authenticated against the workspace's `agent_credentials`; the `memory_read`
and `memory_write` capabilities gate reads and writes per tool.

The MCP surface: `search_people`, `prepare_person_briefing`, `get_context_sources`,
`prepare_person_context`, `get_person` / `get_facts` / `get_interactions` / `get_full_record`,
`list_people`, `get_upcoming_birthdays`, `list_overdue_contacts`, and the write tools
(`create_person`, add/update/remove fact, record/update/remove/reattribute interaction,
contact/summary/name/birthday/cadence, `update_person_batch`, archive/restore).

## Model provider

Briefings need a low-cost model, configured per purpose in **Settings -> Model & provider**:

- **Local `claude` / `codex` CLI** -- reuses the machine's Claude Code / Codex subscription
  auth; no API key. Extended thinking is disabled for these (every task here is compression,
  not reasoning).
- **Anthropic API** or an **OpenAI-compatible endpoint** -- with a stored, encrypted API key.

Month-to-date usage, a spend/token ceiling, and a hard pause switch live under **Settings ->
LLM cost controls**.

## Development

```bash
uv sync
uv run pytest              # needs Docker (spins a postgres testcontainer) or MONTAUK_TEST_DATABASE_URL
uv run ruff check . && uv run ruff format --check && uv run mypy
```
