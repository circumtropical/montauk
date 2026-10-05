# ADR 0005 — Semantic vectors in PostgreSQL

Date: 2026-10-05
Status: Accepted (supersedes ADR 0002 §6)

## Context

ADR 0002 §6 kept the Phase 1 file-based vector index (`vectors.npy` +
`chunks.sqlite`), to be rebuilt from PostgreSQL. It was never wired into a
Phase 2 path: after ADR 0003, `search_people`, `prepare_person_context`, and
briefings ran lexical-only, so vague identity recall ("the robotics guy from
the MIT mixer"), a Phase 1 feature, silently stopped working.

Wiring the file index back in runs into a constraint Phase 1 didn't have:
**two processes write people** — the dashboard (owner edits, transcript
extraction, summaries) and the MCP server (agent writes). A file index owned
by one process can't see the other's writes, and two processes rewriting the
same `.npy` / sqlite pair isn't safe.

## Decision

Store vectors in PostgreSQL, in plain tables — **no pgvector**:

- `semantic_chunks` — one row per chunk (person summary, fact, interaction
  window), embedding as a float32 `bytea`.
- `semantic_person_state` — per person, a `content_hash` over the chunk
  texts + embedding model + dimension + chunking config + index schema
  version.

The index **reconciles on read**: before a search, each person in scope is
re-chunked and hashed; a mismatch re-embeds just that person (inside a
savepoint, under a per-workspace `pg_advisory_xact_lock`, so concurrent
writers serialize). Edits from either process, a model change, or a
chunking change are all picked up without hooking every write path.
Structured-field edits (birthday, company) don't change the hash and don't
re-embed. Similarity stays brute-force cosine in numpy over the workspace's
vectors — fine at hundreds to low thousands of chunks.

The embedding provider is still local (`fastembed`, all-MiniLM-L6-v2), loaded
lazily once per process. If it can't load or embed, retrieval degrades to
lexical-only and reports `semantic_available: false` with a reason; a failed
load is not retried for five minutes.

`montauk semantic status|rebuild` reports coverage and warms the index after
a deploy; neither is needed for correctness.

## Consequences

- Both tables are derived data, cascade-deleted with their person/workspace,
  and safe to truncate at any time.
- Wired now: MCP `search_people` (lexical candidates first, then semantic
  hits with `match_evidence` lines), `prepare_person_context`, and briefing
  evidence selection from the MCP server. Dashboard search and
  dashboard-initiated briefings stay lexical for now.
- The first search after an edit pays the embedding cost for that person
  (tens of milliseconds on CPU for a typical record).
- Revisit pgvector only if transcript-scale search makes an ANN index worth
  the extension.
