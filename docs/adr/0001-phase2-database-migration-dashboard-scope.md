# ADR 0001 — Phase 2 first increment: PostgreSQL canonical store, migration, and a read dashboard

Date: 2026-09-08
Status: Accepted

## Context

The Phase 2 specification (`montauk_phase2_build_and_migration_spec.md`) is
large: PostgreSQL cutover, web dashboard, four inbound connectors, an LLM
extraction/review pipeline, backups, Docker Compose, and a broad test matrix.
Delivering all of it in one pass is not realistic, and several acceptance
criteria (live WhatsApp/Google Messages pairing, container vulnerability
scans, 1M-message performance runs) need real accounts or long runtimes.

The owner scoped this increment explicitly:

> implement the new database, migrate the existing markdown data to it, and
> then give me a web dashboard to view that data. Don't worry about the
> connectors or extraction pipeline yet.

## Decision

This increment implements Section 31 steps 1–6, minus anything connector- or
extraction-specific:

1. **PostgreSQL canonical data model** for identity/auth, relationship memory,
   settings, revisions, and audit (spec §8.1, §8.2, §8.4 partial). The source
   archive (§8.3) and extraction/review tables (`review_proposals`,
   `model_configurations`, `summary_cache`, `backup_runs`) are **deferred** to
   the connector/extraction increment. Their absence does not force a
   data-model rewrite: they attach to `workspaces` and `people` by
   `workspace_id` / `person_id` like every other table.
2. **Workspace scoping from the start** (spec §7). Every tenant row carries
   `workspace_id`; repositories offer no unscoped list/get; tests create ≥2
   workspaces and probe cross-workspace access.
3. **Phase 1 → PostgreSQL migrator** (`montauk migrate-phase2`,
   `montauk verify-phase2-migration`) with dry-run, backup, staged import in a
   transaction, verification report, and golden fixture tests (spec §30, §32.6).
4. **Cutover**: after a successful migration PostgreSQL is the canonical store
   for the dashboard and for the Phase 2 service layer. Markdown/JSON remain
   **export-only**. The Phase 1 Markdown data directory is never dual-written.
5. **Read-first web dashboard** (spec §25): first-run owner/workspace wizard,
   password auth with sessions/CSRF/rate-limiting, home page, people directory,
   person page, per-person and workspace export, settings/health. Write
   operations exposed in this increment are limited to archive/restore and
   field edits that the owner performs directly; connector, review, and
   processing UI are stubs that state they are not yet available.

### Phase 1 MCP server

The existing Phase 1 stdio/HTTP MCP server (`montauk serve`) continues to run
against the Markdown store, unchanged, during this increment. Porting the agent
tool surface to PostgreSQL, the Phase 2 tool catalogue, and OAuth-style agent
authorization (spec §21–23) is the next increment. Until then, the supported
path is: migrate into PostgreSQL, use the dashboard; keep using the Phase 1 MCP
server from the Markdown tree if agent access is needed. This is called out in
the README so the state is not ambiguous.

## Consequences

- Two stores coexist transiently. The migrator and dashboard treat PostgreSQL
  as canonical; the Phase 1 tree is a read-only source and rollback anchor.
- The 500+ Phase 1 tests keep running unchanged. New Phase 2 code gets its own
  test layers (unit, repository, migration, web) gated behind a PostgreSQL
  testcontainer.
- Follow-up increments add the source archive, connectors, extraction/review,
  the Phase 2 MCP surface, backups, and Docker Compose.
