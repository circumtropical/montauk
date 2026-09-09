# ADR 0003 — Retire the Phase 1 Markdown stack

Date: 2026-09-09
Status: Accepted

## Context

Phase 1 stored the canonical record as a directory of Markdown person files and
served it to agents through a stdio / streamable-HTTP MCP server (`montauk
serve`). Phase 2 replaced the store with PostgreSQL, added a web dashboard, a
Phase 1 → PostgreSQL migrator, and a new Postgres-backed MCP server (`montauk
mcp`). Once the real deployment had migrated and the agent was pointed at the
new endpoint, the Phase 1 server was stopped and its Markdown tree kept only as
a cold backup — but ~4,000 lines of source and ~400 tests for it stayed in the
tree, along with a duplicate `person_to_markdown`, a YAML config loader nothing
loaded, and per-startup git snapshotting.

## Decision

Delete the Phase 1 stack:

- **Removed:** `server.py`, `tools_core.py`, `tools_ops.py`, `http_app.py`,
  `write_queue.py`, `git_snapshot.py`, `markdown_store.py`, `reconciliation.py`,
  `migration.py` (name-derived-ID cutover), `sqlite_index.py`, `bootstrap.py`,
  `auth.py` (Phase 1 `CredentialStore`), `logging_config.py`, and
  `phase2_migration.py` with its `montauk migrate` commands. `montauk` now
  exposes only `db`, `dashboard`, and `mcp`. `config.py` is down to
  `RetrievalConfig`.
- **Kept:** `person_context.py` (the retrieval engine — used by briefings and
  `prepare_person_context`) and `semantic_index.py` + `embeddings/` (the local
  vector index). Semantic search is not wired into any Phase 2 path yet
  (retrieval is BM25-only); the code stays as the starting point for a future
  pgvector-backed revival. `person_to_markdown` moved into `exporters/markdown.py`
  as a self-contained serializer for `get_full_record` and the briefing-cache
  fingerprint.
- The `legacy_migration_runs` table and its read-only dashboard display stay: it
  is an audit record of an event that happened, not live machinery.

Re-running a Phase 1 import now means reviving the deleted migrator from history.
Given migration is a one-time event that has completed, that trade is acceptable.

## Consequences

- The repo drops from ~7,000 to ~3,000 lines of `src`, and the test suite from
  ~700 to ~315 cases. mypy's strict `files` list widens (the untyped Phase 1
  modules are gone); `ruff format --check` and `ruff check` now cover the whole
  repo in CI.
- The Markdown format is no longer a supported storage or ingestion path. The
  `examples/` Markdown fixtures and `config/config.example.yaml` are removed.
