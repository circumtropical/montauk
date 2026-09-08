# Montauk

**Phase 1 Build Specification**

*Montauk — relationship memory for personal agents.*

Specification date: August 30, 2026

**Status: Phase 1 built. This document has been revised retroactively (September 2, 2026) to describe what was actually implemented, as the starting point for a Phase 2 specification.**

> **As-built note.** The original document was a pre-implementation spec. During
> the build, a number of details were decided or changed. Those decisions are now
> folded into the relevant sections below; where an implementation choice differs
> from the original intent, or where a listed Phase 1 requirement was ultimately
> deferred, the text says so explicitly. Section 40 ("Build Decisions and
> Deviations") collects the notable ones in one place.

## 1. Executive Summary

Montauk is an open-source MCP server that gives a personal-assistant agent a durable, private, human-readable memory of the people in one human user's life. The server is intentionally not the intelligent assistant. In Phase 1, external agents interpret conversations, dictated notes, transactions, email, or other source material and submit concise facts and interaction records to the MCP server. The server stores, validates, indexes, retrieves, and protects that information.

The canonical database is a directory of Markdown person files. SQLite and a semantic vector index are derived acceleration layers that can be deleted and rebuilt without data loss. The system supports multiple authorized agents, serialized writes, targeted or full-record retrieval, fuzzy identity search, contact-cadence queries, birthdays, provenance, Git-based history, and direct manual Markdown editing.

The design favors simplicity, inspectability, portability, privacy, and reliable agent use over CRM-style UI features or autonomous inference.

## 2. Product Goals

- Provide a reliable persistent memory store about actual people known to one human owner.

- Let multiple MCP-compatible agents read from and, when authorized, write to the same relationship repository.

- Make the canonical data directly readable and editable by a human without proprietary tooling.

- Support both exact retrieval and vague 'who was that person?' recall.

- Track interactions so agents can identify people who have not been contacted recently.

- Support birthdays and desired contact cadence as deterministic structured queries.

- Preserve provenance and epistemic confidence without bloating records with raw source material.

- Be secure enough for highly sensitive personal notes, including remote-agent deployments.

- Be straightforward to deploy locally or on a VPS and suitable as a public GitHub portfolio project.

## 3. Explicit Non-Goals for Phase 1

- No ingestion or storage of full raw email, WhatsApp, Telegram, meeting transcripts, or dictated audio.

- No built-in LLM fact extraction, identity inference, summarization, or conversational prose generation.

- No multi-human tenancy within one deployment.

- No organizations, companies, families, boats, or groups as first-class records; only people are first-class entities.

- No web UI.

- No attachment, image, profile-photo, PDF, or blob storage.

- No automatic reciprocal relationship updates between person files.

- No arbitrary user-defined fact categories or schema fields.

- No routine hard-delete tool exposed to agents.

- No automatic Git push.

- No filesystem watcher for manual edits; manual edits take effect after server restart.

## 4. Core Architecture

```text
External Personal Assistant Agent(s)
        |
        | MCP over local transport or authenticated HTTPS
        v
+-----------------------------------------+
| Montauk Relationship Memory (Python)    |
| - auth / roles                          |
| - validation                            |
| - serialized write queue                |
| - retrieval/search tools                |
| - reconciliation                        |
+-------------------+---------------------+
                    |
          +---------+----------+
          |                    |
          v                    v
 Canonical Markdown       Derived Indexes
 people/*.md              SQLite metadata/search
 archive/*.md             Vector embeddings/index
          |
          v
 Daily local Git snapshot
```

Architectural invariant: Markdown is authoritative. SQLite and vector data are derived and rebuildable. If a derived index disagrees with Markdown, Markdown wins.

## 5. Deployment and Ownership Model

- One server deployment belongs to exactly one human owner.

- Many agents may connect to that deployment.

- Other humans deploy separate instances from the same generic open-source repository.

- All writes are serialized by the server. Concurrent agents wait their turn rather than modifying person files concurrently.

- A single atomic batch may modify one person only. Multi-person events are represented by separate serialized calls.

**As built.** A deployment is created with `montauk init --data-dir PATH`, which
scaffolds the data directory and a **local** private git repository for the
canonical Markdown (branch `main`, initial commit, derived/secret paths
gitignored). The operator wires up their own private remote and push cadence;
Montauk never adds a remote or pushes. The server is then run with
`montauk serve` (stdio for a single local agent host, or authenticated
streamable-HTTP for multiple remote agents).

## 6. Repository Layout

```text
montauk-mcp/                         # public source repository (MIT)
  config/
    config.example.yaml
  data/                              # wholesale-gitignored in the source repo
  examples/
    simpsons/                        # synthetic fixture: identity/index/edge-case coverage
      people/  archive/  person-id-sequence.json
    dating/                          # synthetic fixture: prepare_person_context coverage
      people/  person-id-sequence.json
  src/montauk/
  tests/
  .github/workflows/ci.yml
  pyproject.toml
  uv.lock
  Dockerfile
  LICENSE
  README.md

<data_dir>/                          # a deployment's data directory, anywhere on disk
  people/
    P0001.md
    P0002.md
  archive/
    P0009.md
  person-id-sequence.json            # canonical, git-tracked person-ID high-water mark
  config.yaml                        # written by `montauk init` (non-secret settings)
  README.md                          # written by `montauk init` (private-repo notice)
  .gitignore                         # written by `montauk init` / git-snapshot
  index/
    relationships.sqlite             # derived relational index
    vectors/
      vectors.npy                    # derived: float32 embedding matrix
      chunks.sqlite                  # derived: chunk metadata + fingerprints
  auth/
    credentials.sqlite               # agent credentials (SECRET; never derived, never git-tracked)
  logs/
    montauk.log                      # rotating application log
  validation-report.json             # regenerated every startup / validate
  .montauk.lock                      # advisory write lock
  id-migration-map.json              # only present during/after `montauk migrate-ids`
```

A deployment's data directory is chosen via `--data-dir` or the config's `data_dir`
and normally lives **outside** the source checkout (e.g. `~/.local/share/montauk`,
`/var/lib/montauk`, or a Docker volume). `montauk init` scaffolds it, including a
**local** git repository on branch `main` whose `.gitignore` excludes `index/`,
`auth/`, `logs/`, `validation-report.json`, `.montauk.lock`, and the migration
map/backups. The source repo's own top-level `data/` is wholesale-gitignored so
private relationship data is never committed to a public remote. Montauk never
adds a git remote and never pushes (section 29).

## 7. Person Identifiers

Every person has a permanent, generic, deployment-local identifier. IDs are **not** derived from names; they are sequential and opaque.

```text
P0001
P0002
P0003
```

- Format: uppercase `P` followed by a zero-padded number, initially four digits, growing naturally past `P9999` (`P10000`, ...). Validate with semantics equivalent to `^P[0-9]{4,}$`.

- The server assigns the ID. Agents never supply or choose it under normal creation flows.

- IDs are never based on, and never change with, a person's name.

- IDs are immutable and are never reused -- not after archival, not after deletion, not after a failed creation (gaps are acceptable).

- Allocation is concurrency-safe: a persisted high-water mark (`person-id-sequence.json`) is advanced *before* the new person file is written, under the single serialized write lock. It is canonical, git-tracked state -- never rebuilt from the Markdown files, only self-healed upward on startup to cover any IDs added by hand.

- Canonical filenames are `people/<person_id>.md`. The file is **not** renamed when the person's display name changes.

- Names and filenames are not relied upon as identifiers.

- Relationships and API mutations reference person IDs.

- Identity resolution is separate from mutation: agents search/resolve first, then write using the returned person ID.

> **Identity vs. name.** A person's ID represents their identity and never changes. Their name represents the best information currently known about them and may change without replacing the person record. Never archive and recreate a person merely to correct or complete their name -- use `update_person_name`.

## 8. Canonical Markdown Person Schema

**As built.** The canonical file is YAML front matter followed by the six fixed
`## ` category headings and a `## Interactions` heading, in that fixed order. All
seven headings are always emitted, even when empty. Each category heading's body
is a YAML list of fact mappings; under `## Interactions`, each interaction is a
`### int-N` sub-heading whose body is a YAML list of single-key field mappings.
The `# Name` title line is regenerated from `name` on every write. The server
never edits a file in place: it rebuilds the whole file canonically on each write,
with facts and interactions sorted by their numeric local ID, so an unchanged
`Person` always re-serializes byte-identically (this keeps content-hash index
reconciliation and Git diffs stable). Front-matter `contact` is a nested mapping
(`emails`, `phones`, `address`, `messaging`). The following illustrates the
schema; the real serializer's spacing/quoting is deterministic but not identical
to this hand-written sample.

```text
---
id: P0042
name: Mike Chen
aliases:
  - Michael Chen
  - Mike
birthday: 1982-04-17
location: "Normally Boston; often vacations in Puerto Rico; last known in New Zealand"
company: Acme Robotics
job_title: VP Engineering
desired_contact_cadence_days: 90
summary: >
  Stanford acquaintance working in robotics. Met through a technology event.
contact:
  emails:
    - mike@example.com
  phones:
    - "+1-207-555-0100"
  address: "..."
  messaging:
    telegram: "@mikechen"
---

# Mike Chen

## Family

- id: fact-1
  date: 2025
  confidence: high
  text: Mike is married to Jane.
  sources:
    - type: interaction
      id: int-1

## Work & Education

- id: fact-2
  date: 2026-08
  confidence: medium
  text: Mike may be considering leaving Acme Robotics.
  sources:
    - type: agent
      id: personal-assistant

## Interests

...

## Relationship with User

- id: fact-3
  date: 2025
  confidence: high
  text: Met Mike at a Stanford-related robotics event.

## Life Events

...

## General Notes

...

## Interactions

### int-1
- date: 2026-08-12
- channel: in-person
- connection_level: 4
- summary: Had lunch with Mike.
- sources:
  - type: agent
    id: personal-assistant
```

## 9. Structured Person Fields

| Field | Required? | Meaning / Rules |
| --- | --- | --- |
| id | Yes | Permanent generic deployment-local person ID (`P0001`). Never name-derived; never changes; never reused. |
| name | Yes | Best currently known display name. Mutable. May be partial, approximate, or misspelled when that is all that is known. Not split into first/last. |
| aliases | No | Alternate lookup forms: former display names, partial names, alternate spellings, nicknames, known misspellings. Normalized (trim + collapse whitespace + case-fold) for comparison/indexing; the human-readable spelling is preserved for display. De-duplicated under that normalization. The current display name is not stored redundantly as an alias. |
| birthday | No | Structured birthday; year may be omitted if unknown. |
| location | No | Loose free-text current/general location description. |
| company | No | Current company; employment history belongs in facts. |
| job_title | No | Current job title. |
| desired_contact_cadence_days | No | Blank means no proactive keep-in-touch priority. |
| summary | No | Short agent-authored identifying overview; convenient, not authoritative over detailed facts. |
| contact | No | Current contact methods only: emails, phones, home/mailing address, messaging/social handles. |

Structured contact fields represent currently valid information. Obsolete addresses, numbers, and handles are not retained merely for history. If old information is relationship-relevant, it may appear as a narrative fact.

## 10. Fact Model

- Every fact belongs to exactly one fixed narrative category and appears only once.

- Each fact has a short ID unique within that person file: fact-1, fact-2, etc.

- Fact text is concise natural language.

- Dates support variable precision: YYYY-MM-DD, YYYY-MM, YYYY, or blank/unknown.

- Confidence represents epistemic certainty only. Recommended values: high, medium, low. High is the default.

- The wording itself must preserve uncertainty or attribution; confidence metadata is not a substitute for careful language.

- Provenance is optional.

- Incorrect facts are corrected or removed in the canonical Markdown rather than retained as superseded misinformation. Git provides edit history.

## 11. Fixed Narrative Categories

Phase 1 uses a fixed schema to prevent category proliferation. Recommended initial categories:

- Family

- Work & Education

- Interests

- Relationship with User

- Life Events

- General Notes

The category list should be defined centrally in code/schema and documented. General Notes is the catch-all. New arbitrary categories are not supported in Phase 1. 'How and when we met' is narrative information, normally placed under Relationship with User.

## 12. Interaction Model

Interactions are first-class records even when they produce no new facts. They support relationship history and contact-recency calculations.

| Field | Required? | Notes |
| --- | --- | --- |
| id | Yes | Short person-local ID such as int-42, generated and stable. Independent of the interaction's participants and descriptive content: correcting those never changes the id. Does not use the person-ID sequence. |
| date | Yes | Variable precision date: YYYY-MM-DD, YYYY-MM, or YYYY. |
| channel | No | Free-form text such as in-person, WhatsApp, phone, email, etc. |
| connection_level | No | Placeholder integer, initially 1-6. Semantics intentionally undefined in Phase 1. |
| summary | No | Concise interaction summary; raw conversation text is not stored. |
| sources | No | Compact provenance references. |

Phase 1 cadence calculations use interaction dates, not connection_level. Future versions may define connection-level semantics.

### 12.1 Correcting Interactions

Correcting inaccurate data is not erasing history. Accurate historical interactions must not be removed merely because they are old, inconvenient, sensitive, or no longer relevant.

- `update_interaction(person_id, interaction_id, ...)` corrects a recorded interaction whose details are wrong. Omitted fields are unchanged; the complete resulting interaction is validated; the `interaction_id` never changes.

- A wrong participant is corrected by moving the interaction: `update_interaction(..., move_to_person_id=...)` allocates a fresh interaction id on the corrected person, removes the interaction entirely from the former person (no tombstone, voided record, or searchable trace), and reindexes both records atomically.

- `remove_interaction(person_id, interaction_id, correction_reason)` is only for a record that is itself erroneous -- it never happened, it was created by mistake, or it duplicates another. A non-empty `correction_reason` is required. The interaction and all references to it are removed from active data and every index, with no voided copy retained. The operational log records only the ids and that a correction occurred, never the erroneous contents (spec section 28).

Choosing the operation: detail wrong but event occurred -> `update_interaction`; wrong person -> `update_interaction` with `move_to_person_id`; record should not exist / is a duplicate -> `remove_interaction`; accurate but old/sensitive/inconvenient -> leave it.

## 13. Provenance Model

Facts and interactions may contain zero or more compact sources. A source contains only a type and ID.

```text
sources:
  - type: interaction
    id: int-42
  - type: agent
    id: personal-assistant
```

Possible source types include interaction, agent, manual, import, or future source classes. No source explanation text is required in Phase 1. Provenance is optional because direct manual Markdown editing is a supported workflow.

## 14. Confidence Semantics

| Value | Interpretation |
| --- | --- |
| high | Directly asserted, strongly supported, or otherwise highly certain. |
| medium | Plausible inference or incomplete support. |
| low | Tentative hypothesis or weakly supported inference. |

External agents may explicitly submit medium- or low-confidence facts in Phase 1. The MCP server stores the supplied confidence but does not independently reason about it. Phase 2 may add server-side LLM inference that assigns confidence.

## 15. Relationships Between People

- A person file may mention another person's name in narrative text.

- Where useful, a relationship may optionally reference another existing person ID.

- Links are one-way assertions only.

- The server does not automatically create, mirror, or reconcile reciprocal relationships.

- Not every spouse, child, or relative must have a separate person record.

- An explicit `related_person_id` must be a canonical generic person ID (`^P[0-9]{4,}$`) that resolves to an existing active or archived person, and must not be the record's own id. A shared or similar display name never makes two records the same person; Montauk does not merge people or reject a write because a name is reused. Moving information between people or merging identities requires explicit user direction (a `merge_people` operation is out of scope for Phase 1).

### 15.1 Record-Scoped Relevance and Unrelated People

Governing principle: **a source may be about several people; a Montauk record is about exactly one person.**

- Each person record must contain only information directly relevant to that person.

- Conversational or source-level co-occurrence does not establish a relationship. Two people appearing in the same conversation, note, meeting, message, or source event are not thereby related, and the server never infers a relationship from co-occurrence alone.

- A fact, summary, note, or interaction stored for one person may identify another person only when the source supports it: the person of record knows / has met / communicated with / otherwise has a direct relationship with the other person; or the source describes an actual interaction involving both; or the information explicitly concerns the relationship between the two; or naming the other person is necessary to understand a fact directly about the person of record.

- A legitimate relationship or interaction can be a single meeting, introduction, professional contact, transaction, or shared activity -- it need not be friendship or a long-standing tie. The rule prevents unsupported associations; it does not require closeness.

- When one source contains information about multiple unrelated people, separate the information by subject and produce an independent one-person update for each person, including only subject-relevant information and omitting unrelated surrounding context.

- If the relationship or relevance is uncertain, omit the cross-person reference or ask the user to clarify.

This rule is communicated to agents through the server instructions (`RECORD SCOPING`) and repeated in the descriptions of every mutation tool that accepts narrative content. Structurally, every mutation still targets exactly one `person_id`, a one-person batch cannot mutate multiple records, and an explicit `related_person_id` must resolve to a real existing person (spec section 15); the semantic judgement of relevance is the agent's, guided by that published text.

## 16. Archive Semantics

- Normal lifecycle removal is archive, not delete.

- Archiving moves the Markdown file from people/ to archive/.

- Archived people are removed from SQLite and vector indexes and disappear from ordinary search, birthday, and cadence queries.

- Explicit archive-list/read operations may still access archived files.

- Moving a valid file back to people/ and restarting/reconciling restores it to active indexing.

- Permanent privacy erasure is an administrative/manual procedure, not a routine MCP agent tool. Because Git retains history, true erasure requires Git-history handling as well.

## 17. Derived SQLite Index

SQLite is the recommended Phase 1 relational index because this is a single-user deployment and SQLite avoids a separate database service. The design should keep a storage abstraction sufficiently clean that PostgreSQL can be supported later if desired.

Recommended indexed data:

- person_id, display name, aliases

- birthday

- location

- company and job title

- desired contact cadence

- last interaction date (derived from interaction records)

- summary

- current contact details as useful for exact lookup

- file path and content hash/fingerprint

- index/reconciliation metadata

Do not persist 'days since last interaction' as canonical index state. Compute it from last_interaction_date at query time so it never goes stale.

**As built.** One `people` table keyed by `person_id` (name, `aliases_json`,
birthday month/day/year, location, company, job_title, cadence, summary,
`contact_*_json`, `last_interaction_at`, `file_path`, `content_hash`,
`updated_at`) plus an `index_meta` key/value table (`schema_version`,
`last_reconciliation_at`). `last_interaction_at` stores the *latest* calendar date
consistent with a partial-precision interaction date. WAL journal mode; single
writer, serialized through the write queue. `days_since_last_interaction` is
computed at query time and is not a column, as specified.

## 18. Startup Reconciliation

1. Scan active Markdown files in people/.

2. Validate each file independently.

3. For valid files, compare a stored content hash/fingerprint with the SQLite index.

4. Insert/re-index new files and re-index changed files.

5. Remove index entries whose active files no longer exist or have moved to archive/.

6. Rebuild or update semantic chunks for changed records.

7. Skip malformed records, record clear validation errors, and continue startup.

8. Publish health state as healthy or degraded.

9. Self-heal the person-ID high-water mark upward to cover any generic ID already present in people/ or archive/ (e.g. a file added by hand). It is only ever raised, never lowered or rebuilt from the files.

Manual Markdown edits are expected to be followed by a server restart. Phase 1 does not need a filesystem watcher.

## 19. Semantic / Vector Search

Semantic search is a Phase 1 feature because vague identity recall is a core use case: e.g., 'Who was the robotics guy I met at an MIT mixer about a year ago?'

- Index small chunks rather than whole person files.

- Embed the short person summary, individual facts, and interaction summaries.

- Do not separately embed deterministic structured fields such as birthdays or phone numbers.

- Each vector chunk carries person_id, chunk type, local fact/interaction ID when applicable, and source text/snippet.

- Search returns ranked candidate people plus match evidence.

- When multiple candidates reasonably match, return multiple candidates; do not force a single identity decision.

- The external assistant handles conversational disambiguation with the user.

- Similarity threshold and result limit use server defaults in Phase 1; they may be configurable in YAML for administrators.

**As built.** Vector storage is brute-force cosine similarity over an in-memory
`float32` numpy matrix persisted to `index/vectors/vectors.npy`, with parallel
chunk metadata (id, person_id, chunk type, local id, chunk position, text) in
`index/vectors/chunks.sqlite`. No approximate-nearest-neighbour library (FAISS,
hnswlib, ...) is used: brute force is fast enough at the expected scale (hundreds
to low thousands of chunks for one person's-worth of relationships). `chunks.sqlite`
also holds `vector_meta` (schema version, embedding model name, dimension,
chunking-config fingerprint) and `person_meta` (per-person canonical content hash)
for drift detection. `search_people` runs the SQLite substring/alias/company/
location scan **and**, when the semantic index is present and current, the vector
search, then merges candidates and attaches per-match evidence lines. The default
`search.similarity_threshold` is **0.35**, empirically calibrated for the default
local model (see section 20) — the original illustrative `0.55` dropped genuinely
relevant matches for this model.

### 19.1 Indexable units and interaction chunking

The derived index holds one entry per semantic unit: the current person summary, each individual fact, each relationship (a fact carrying a `related_person_id` -- indexed as its own fact chunk), and each interaction summary. A long interaction summary is split at sentence boundaries into overlapping chunks (`interaction_chunk_tokens` / `interaction_chunk_overlap_tokens`); short ones stay whole; facts and the person summary are never split. Every chunk of an interaction references the same canonical `interaction_id` and records its position (`chunk_index` / `chunk_total`). A whole person file is never embedded as one vector.

Each chunk carries enough internal metadata to fetch its canonical source (person_id, record type, record id, chunk position, content hash, embedding model/version). This is internal index metadata; it is not returned to clients.

### 19.2 Index lifecycle

- After every successful mutation that can affect retrieval (create/rename/summary/fact/relationship/interaction add-update-remove, participant correction, archive/restore), the affected index entries are synchronously updated or removed before the mutation is reported fully successful. Unchanged content hashes skip re-embedding.
- Canonical Markdown is written first (Appendix B). If the derived-index update then fails, the mutation still succeeds and reports `index_update_status: degraded`; retrieval detects the per-person hash mismatch and serves lexical-only for that person until it is reindexed. Known-stale semantic results are never served as current.
- Startup validates schema version, embedding model/dimension fingerprint, chunking-config fingerprint, and per-person content hashes; it removes orphaned entries and re-embeds changed people. A fingerprint mismatch with the **local** provider triggers an automatic rebuild; with a provider whose rebuild is an externally billed operation it degrades to lexical-only and reports that `montauk rebuild-index` is required.

## 20. Embedding Provider Interface

Define an implementation-neutral provider interface and ship one lightweight local embedding implementation as the default. Hosted/API providers may be added as optional adapters.

```text
class EmbeddingProvider(Protocol):
    @property
    def dimension(self) -> int: ...
    def embed(self, texts: list[str]) -> list[list[float]]: ...
```

- Default should run locally on CPU without requiring an API token.

- The provider interface exposes provider identity, model identity, dimension, batch embedding, and (implicitly) failure reporting.

**As built.** The `EmbeddingProvider` Protocol is `dimension: int` plus
`embed(list[str]) -> list[list[float]]`. The one shipped implementation,
`LocalEmbeddingProvider` (`provider = "local"`), runs
**`sentence-transformers/all-MiniLM-L6-v2`** (384-dim) via **`fastembed`**
(ONNX Runtime, no PyTorch, no API token). Model weights are fetched from
Hugging Face Hub once and cached on disk; the Dockerfile pre-bakes them into the
image (`FASTEMBED_CACHE_PATH=/opt/fastembed_cache`) so a fresh container needs no
network on first run. `EmbeddingConfig.provider` is currently typed as
`Literal["local"]` — no hosted adapter ships, and adding one is Phase 2 work; the
config schema, not just the code, would need to change to select it.

- **Privacy:** the default installation sends no person records, facts, relationships, or interactions to a hosted provider. A hosted provider may be used only after an explicit provider selection in config **and** explicit enablement -- finding an API key in the environment is not authorization. There is no silent local-to-hosted fallback. Raw personal text is not logged to diagnose embedding requests; credentials never appear in person files, shareable indexes, logs, or tool output. If the configured semantic provider is unavailable, retrieval continues lexically and reports `semantic_available: false` with a short reason; the request is not failed.

- Model/provider changes trigger vector-index rebuild.

- Embeddings are derived data and may always be regenerated from Markdown.

## 21. Retrieval Behavior

The MCP API must support both narrow and broad reads.

| Capability | Purpose |
| --- | --- |
| resolve/search person | Find IDs from names, aliases, structured metadata, and semantic evidence. |
| get person | Return structured core fields and short summary. |
| get facts | Return facts, optionally narrowed by fixed category. |
| get interactions | Return interaction history, optionally recent subset. |
| get full record | Return the complete canonical Markdown person record on explicit request. |
| birthday queries | Return upcoming/today birthdays deterministically. |
| cadence queries | Return people overdue relative to desired cadence and last interaction. |
| archive reads | Explicitly inspect archived records. |
| validation errors | Let agents inspect skipped/malformed records. |
| health/status | Operational state without exposing sensitive person content. |
| purpose-specific context | Return the evidence from one person's record relevant to a stated question/task, within a token budget. |

### 21.1 Purpose-Specific Person Context

`prepare_person_context(person_id, purpose, detail_level="standard", max_tokens=null)` returns a compact, purpose-specific **evidence packet** -- selected canonical memory, substantially verbatim -- not a complete context dump and not a generated answer. Montauk selects; the calling agent interprets, advises, researches, and judges.

- **Progressive disclosure:** resolve the person (`search_people`) -> `prepare_person_context` with the purpose -> fetch specific cited records (`get_facts` / `get_interactions`) or the whole file (`get_full_record`, for review/export/maintenance) only when more is needed.
- **Hybrid retrieval:** lexical ranking (BM25 over an ephemeral per-person full-text index -- always available, never stale) combined, when the semantic index is up, with vector similarity, plus deterministic signals: record type, section, recency (only for temporal purposes), exact name/quotation/date/place hits, and duplicate suppression. Search never crosses people once `person_id` is resolved. Ranking weights are a separate, testable component -- not buried in the MCP handler. Raw scores are never presented as calibrated probabilities.
- **Selection & diversification:** exact duplicates removed; near-duplicate facts collapsed (canonical text never merged or rewritten); a broad briefing diversified across identity / relationship / interests / history / recent interactions.
- **Output budget:** `brief` ~750, `standard` ~2,000, `comprehensive` ~6,000 tokens of selected evidence text; explicit `max_tokens` overrides within `[retrieval.min_tokens, retrieval.max_tokens]`. Token counts use a conservative over-estimating heuristic (Montauk has no bundled tokenizer). The budget governs the evidence content, not the small response envelope. Over-budget matches are dropped and reported: `truncated: true` with `additional_matching_items`.
- **Response:** `person {id,name}`, `purpose`, `detail_level`, optional `summary {id,text}`, `facts[]` / `relationships[]` (`id`, `text`, plus `related_person_id`, `section`, `date`, and `confidence` only when materially relevant -- default `high` confidence and all storage/index metadata are omitted), `interactions[]` (`id`, `text`, conditional `date` / `channel`), `retrieval {semantic_available, truncated, returned_items, additional_matching_items, approximate_tokens, budget_tokens, note?}`, and an optional `temporal` block (objective elapsed time only -- `last_recorded_interaction`, `days_since_last_recorded_interaction` when the supporting date is full-precision, and the supporting interaction id; never a subjective verdict).
- **Honesty about gaps:** if a specific detail is not stored, the packet simply omits it (e.g. "she has dogs" with no dog name). Montauk generates no advice, recommendations, compatibility judgments, quotations, or missing facts.
- **Lexical-only fallback** is a supported operating mode (no provider configured, model won't load, provider disabled/unavailable, index rebuilding): retrieval still works and `semantic_available` is `false` with a short structured reason.

This first implementation uses hybrid retrieval returning canonical content substantially verbatim. LLM summarization / generative compression and autonomous memory curation are explicit non-goals; the `prepare_person_context` contract is designed so they can be added later without changing it.

**As built** (`person_context.py`, a pure/deterministic/independently-tested module):

- **Purpose analysis** is regex/keyword heuristics, no LLM. It extracts query
  terms (stopworded, with the subject's own name dropped), quoted phrases, and
  proper nouns, and sets boolean intents: `is_temporal`, `wants_events`,
  `wants_present_state`, `wants_preferences`, `is_advisory`, `is_briefing`. These
  drive per-unit ranking boosts and the selection gate.
- **Lexical** ranking is BM25 over an **ephemeral in-memory SQLite FTS5** table
  built per call (`porter unicode61` tokenizer); if the host SQLite lacks FTS5 it
  falls back to normalized term-overlap scoring. Always available, never stale.
- **Semantic** ranking is per-person vector search (`search_person`), never
  crossing to other people. Scores are min-max-scaled within the person's own
  top matches, not treated as calibrated probabilities.
- **Ranking weights** live in a `RankWeights` dataclass (a separate, testable
  component). Selection uses a per-detail-level relevance floor (fraction of the
  top unit's score) and a soft item cap; `comprehensive`/briefing keep everything
  materially relevant and round-robin-diversify across evidence buckets.
- **Token budget** uses the conservative over-estimating heuristic in `tokens.py`
  (max of a word-based and char-based estimate); Montauk bundles no real
  tokenizer.
- The `temporal` block is emitted only for temporal purposes or `comprehensive`;
  elapsed days are computed only when the supporting interaction date is
  full-precision.

## 22. Identity Resolution

Identity resolution is deliberately separated from mutation. The MCP server supplies candidates; the external intelligent agent makes the conversational choice.

```text
search_people("Mike Chen from Stanford who I met about a year ago")

[
  {
    "person_id": "P0007",
    "name": "Mike Chen",
    "summary": "MIT classmate; works in robotics",
    "match_evidence": ["Met at MIT alumni mixer in 2025"]
  },
  {
    "person_id": "P0042",
    "name": "Mike Chen",
    "summary": "Stanford alum; startup founder",
    "match_evidence": ["Met at robotics conference in 2025"]
  }
]
```

The agent may then ask the user which Mike was intended and use the selected person_id for subsequent writes. Identical display names are expected and allowed; user-facing output may show `Mike (P0042)` to disambiguate.

## 23. Write Model and Atomicity

- Broad reads are allowed; writes should be narrow and structured.

- Provide operations such as create_person, add_fact, update_fact, remove_fact, record_interaction, update_interaction, remove_interaction, update_contact_details, update_summary, update_person_name, set_birthday, set_contact_cadence, and archive_person.

- `create_person` assigns the generic ID; the caller supplies only the best known name (which may be incomplete). Any existing people with the same name are returned as `possible_duplicates` -- advisory only, never auto-merged, never a reason to reject.

- Also provide a preferred one-person batch update operation for related changes discovered together.

- A batch may add an interaction, add/update/remove facts, rename the person (`set_name`), and update structured fields for one person. A batch still targets exactly one `person_id` and cannot mutate multiple person records; a source about several unrelated people is split into a separate batch per person.

- Validate the entire proposed update before writing.

- Apply all changes or none of them.

- MCP-originated writes must never leave a person file invalid.

- Multi-person changes require separate calls.

- All writes pass through one server-side serialized queue/lock.

## 24. Suggested MCP Tool Surface

Exact MCP naming can be adjusted to current SDK conventions, but the Phase 1 capability surface should approximate:

Every registered MCP tool must be self-describing. Its name, description, input schema, and output schema should give a newly connected model enough local information to decide when and how to call it. Tool descriptions should state important preconditions and ambiguity behavior, not merely restate the function name.

```text
# Identity / search / context
search_people(query)
get_person(person_id)
prepare_person_context(person_id, purpose, detail_level="standard", max_tokens=None)
get_facts(person_id, category=None)
get_interactions(person_id, ...)
get_full_record(person_id)              # explicit review / export / maintenance only
get_upcoming_birthdays(...)
list_overdue_contacts(...)

# Writes
create_person(...)                       # server assigns a generic P0001-style id
update_person_name(person_id, name, retain_previous_as_alias=true, aliases_to_add=[], aliases_to_remove=[])
add_fact(person_id, ...)
update_fact(person_id, fact_id, ...)
remove_fact(person_id, fact_id)
record_interaction(person_id, ...)
update_interaction(person_id, interaction_id, ..., move_to_person_id=None, correction_reason=None)
remove_interaction(person_id, interaction_id, correction_reason)
update_contact_details(person_id, ...)
update_summary(person_id, ...)
set_birthday(person_id, ...)
set_contact_cadence(person_id, ...)
update_person_batch(person_id, operations=[...])
archive_person(person_id)

# Operations / diagnostics
validate_repository()
get_validation_errors()
get_health_status()
list_archived_people()
get_archived_person(person_id)
```

Read-only credentials must be prevented from invoking mutation tools server-side.

**As built.** The registered tool names match the list above exactly, in three
groups (`register_core_tools`, `register_batch_tools`, `register_ops_tools`), with
these notes:

- `update_person_batch(person_id, operations=[...])` accepts a discriminated union
  of op types: `add_fact`, `update_fact`, `remove_fact`, `record_interaction`,
  `update_contact_details`, `update_summary`, `set_name`, `set_birthday`,
  `set_contact_cadence`. It does **not** include `update_interaction`,
  `remove_interaction`, or `archive_person` — those stay single-purpose tools.
- `get_interactions(person_id, limit=None)` returns interactions most-recent-first;
  `limit` takes the N most recent.
- There is no un-archive / restore tool. Restoring an archived person is a manual
  file move plus a restart or `rebuild-index` (section 16).
- `search_people` does not raise `AMBIGUOUS`; it returns the candidate list and
  leaves disambiguation to the caller (section 22).
- MCP prompts (section 25.1) are not implemented — not needed for Phase 1.
- MCP resources are not used; `get_full_record` / `get_archived_person` return the
  canonical Markdown as a tool-result string.

## 25. Agent Integration Contract

The MCP interface is not only a function endpoint; it is the discoverable contract by which an arbitrary compatible assistant learns how to use the Montauk server. Phase 1 should explicitly define both server-level usage instructions and high-quality per-tool descriptions/schemas.

### 25.1 Protocol Discovery and Instruction Layers

Target MCP protocol revision: 2026-07-28 or newer compatible revisions. In this protocol generation, clients can use server discovery to learn server capabilities and optional natural-language instructions. Tool catalogs remain discoverable independently through the MCP tools interface, including each tool name, description, input schema, and output schema. Implement backward compatibility only where the chosen Python MCP SDK makes it practical; do not design Phase 1 around the older stateful initialization handshake.

**As built.** The server is built on the `mcp` Python SDK, constrained to
`mcp>=2.1,<3`, via `MCPServer(name="montauk", instructions=SERVER_INSTRUCTIONS)`
from `mcp.server.mcpserver`. Tools are registered with the `@server.tool`
decorator; input/output schemas are derived from the handler's type hints and
Pydantic models (`tool_types.py`), and pydantic rejects malformed calls before
handler logic. Two transports are supported:

- **stdio** — `mcp_server.run_stdio_async()`. Exactly one client; its identity is
  resolved once at startup from `MONTAUK_AGENT_TOKEN` (section 30).
- **streamable HTTP** — `server.streamable_http_app(...)` mounted under Starlette
  and served by uvicorn. The MCP endpoint path is **`/mcp`**. Per-request
  `Authorization: Bearer` headers are read via `Context.headers`.

Server instructions are published through the SDK's `instructions` field; the
tool catalog (names, descriptions, JSON schemas) is discoverable independently.
Typed tool errors (`errors.py`) subclass the SDK's `ToolError` and carry a
machine-parseable `code:` prefix.

- Server instructions: concise global guidance explaining how the Montauk tools fit together and the expected workflow.

- Tool definitions: specific descriptions and schemas that explain the purpose, preconditions, inputs, outputs, and ambiguity behavior of each operation.

- MCP prompts: optional user-selected workflow templates, if useful later; prompts are not the primary mechanism for teaching an autonomous agent how to use the server.

- Deterministic enforcement: security, authorization, data validation, atomicity, and other correctness-critical rules are enforced in code rather than entrusted to natural-language instructions.

### 25.2 Recommended Server Instructions

The server should publish concise instructions equivalent in meaning to the following. Keep the production version compact enough to be reliably included in model context.

```text
Montauk is the user's persistent relationship memory about people they know.

IDENTITY AND NAMES
- If you already have a person_id, use it directly.
- A person_id is a permanent system-generated identity such as P0001. Never derive it from a name, change it after creation, or reuse it.
- A person's name is the best currently known display name. It may be partial, approximate, misspelled, or later corrected.
- Never archive and recreate a person merely to correct or complete their name. Use update_person_name and keep useful former or alternate names as aliases.
- If person_id is unknown, search for the person before creating or modifying a record.
- Search may return multiple plausible people. Do not guess when identity is ambiguous; surface the candidates and ask the user to clarify.
- Do not create a duplicate person merely because identity is uncertain. Similar or identical names do not identify the same person; ask before merging identities or moving information between people.

RECORDING INFORMATION
- Store concise facts and interaction summaries, not raw conversations, emails, transcripts, or message dumps.
- Record a meaningful interaction even if it produced no new facts.
- When one event produces several related changes for one person, prefer the atomic one-person batch update.
- Use high confidence for directly stated or strongly supported facts; use medium or low confidence for genuine inference or uncertainty.

INTERACTION CORRECTIONS
- Use update_interaction when an interaction occurred but its participants or other details are inaccurate.
- If an interaction was attributed to the wrong person, correct its participant (move_to_person_id) so the incorrect person retains no interaction record or searchable association.
- Use remove_interaction only when the interaction record itself is erroneous, never occurred, or duplicates another record.
- Correcting inaccurate data is not erasing history. Never remove an accurate interaction merely because it is old, inconvenient, sensitive, or no longer relevant.

RECORD SCOPING
- Keep each record scoped to the person of record.
- Mention another person only when that person has a direct relationship or interaction with the person of record, or when the reference is necessary to understand a fact directly about the person of record.
- People appearing in the same conversation or source material are not necessarily related. Never infer a relationship from co-occurrence.
- When one source discusses several unrelated people, separate the information by subject and update each person independently.
- Omit unrelated surrounding context. If relevance is uncertain, omit the reference or ask the user to clarify.

RETRIEVAL
- Resolve the person first, then use prepare_person_context with the actual question or task as `purpose` to get a bounded set of relevant facts, relationships, and interactions.
- Treat the result as evidence from stored memory, not as Montauk's advice or a generated answer. Montauk does not give advice, recommendations, compatibility judgments, or quotations -- the calling assistant does.
- If the response is truncated or lacks an exact detail, narrow the purpose or retrieve the cited records. Do not invent names, quotations, dates, or facts.
- Retrieve a complete raw record only for explicit review, export, or maintenance.

CORRECTIONS
- Correct or remove information that is discovered to be wrong. Do not preserve misinformation as an active superseded fact solely for history; Git provides edit history.

BOUNDARIES
- Only people are first-class records in Phase 1.
- This server stores and retrieves relationship memory; the calling assistant is responsible for interpretation, conversational disambiguation, and prose generation.
```

### 25.3 Tool Description Requirements

Critical workflow hints should be repeated in the descriptions of the tools to which they apply. MCP hosts may differ in how prominently they expose server instructions to the model, so correctness should not depend on the global instructions being seen or followed perfectly.

- search_people should explicitly say that it accepts vague identifying details and may return multiple plausible candidates with match evidence.

- create_person should explicitly warn callers to search first when there is any possibility that the person already exists, and say that Montauk assigns the permanent generic person_id while the caller supplies only the best known name (which may be incomplete). It must not imply that a shared name blocks creation.

- update_person_name must say it changes the existing person's display name in place without changing identity or recreating the record, retains the previous name as an alias by default, and applies aliases_to_add/aliases_to_remove atomically.

- update_interaction must explain when to correct fields versus participant attribution (move_to_person_id), and that a corrected participant leaves no record or searchable association on the former person.

- remove_interaction must state that it is only for erroneous or duplicate records, requires a correction_reason, and must not be used to erase accurate history that is merely old, sensitive, or inconvenient.

- archive_person should explicitly say it is not the mechanism for correcting a name.

- Mutation tools should require person_id rather than accepting a free-form name as identity.

- update_person_batch should state that it is atomic, applies to exactly one person, validates the whole batch first, and is preferred for multiple related changes from one source event. It should also state that a source about several unrelated people must be split into a separate batch per person.

- Every mutation tool that accepts narrative content (add_fact, update_fact, record_interaction, update_summary, update_person_batch, create_person's initial summary) should repeat the record-scoping rule from section 15.1: content must be directly relevant to the target person, and another person may be named only on source-supported relevance, never on co-occurrence. record_interaction should additionally state that unrelated people discussed in the surrounding source must not appear in the interaction summary.

- get_person and get_facts should make clear that targeted retrieval is preferred when the caller does not need the complete record.

- get_full_record should state that it returns the complete canonical person record and should be used intentionally because it may consume substantially more model context.

- record_interaction should state that an interaction can be worth recording even when no new facts were learned.

- archive_person should make clear that archive is reversible and is not equivalent to privacy erasure.

- All schemas should be explicit enough that invalid calls are rejected before handler logic where the MCP SDK supports schema validation.

### 25.4 Example Tool Descriptions

```text
search_people
  Search active relationship memory using a name, alias, event, location,
  employer, date, or other remembered detail. Returns ranked candidates
  with match evidence. Multiple candidates are expected when identity is
  ambiguous; the caller should ask the user to clarify rather than guess.

create_person
  Create a new person record. Search first if the person might already
  exist. Do not use this tool to resolve identity uncertainty.

record_interaction
  Record a concise interaction summary for a known person_id. Use this
  for meaningful contact even when no new facts were learned. Do not send
  a raw transcript or message thread.

update_person_batch
  Atomically apply multiple related changes to exactly one person. The
  entire batch is validated before any change is written. Prefer this
  when one interaction or source event produces several updates.

get_full_record
  Return the complete canonical record for a known person_id. Prefer
  targeted retrieval tools for narrow questions to reduce context use.
```

### 25.5 Integration Reliability Principle

Natural-language instructions improve tool use but do not provide a security or integrity boundary. The server must independently enforce authentication, authorization, required IDs, one-person batch scope, validation, archive rules, serialized writes, and other invariants. An agent that ignores instructions should receive a safe error rather than being able to corrupt or expose the repository.

## 26. Contact Cadence and Birthday Logic

- desired_contact_cadence_days is nullable. Null means no proactive keep-in-touch priority.

- last interaction is derived from interaction records and indexed in SQLite.

- Overdue status compares current date against last interaction date and desired cadence.

- If a person has a cadence but no known interaction, define them as eligible for an 'unknown/never contacted' result rather than inventing a date.

- birthday is a structured field. The birth year may be unknown.

- The MCP server exposes deterministic birthday queries; scheduling actual notifications remains the responsibility of the external assistant/automation layer in Phase 1.

## 27. Validation

Validation occurs on startup and before every MCP-originated write. It is also exposed through a read-only validate_repository operation and admin CLI command.

Minimum checks:

- Required person ID and display name.

- Unique active person IDs.

- Valid variable-precision dates.

- Allowed confidence values.

- Unique fact IDs within a person.

- Unique interaction IDs within a person.

- Valid structured contact shapes.

- Valid cadence values.

- Relationship references, when present, point to valid person IDs or are clearly allowed as unresolved names.

- Front matter and required Markdown structure parse correctly.

A malformed manual file must not prevent server startup. Skip it, omit it from indexes, mark health as degraded, and record a specific error.

**As built.** Each `ValidationIssue` has a severity. **Error** severity (excludes
the person from the valid set, marks the repo degraded): unreadable file, YAML/
Markdown format error, Pydantic domain-validation error, duplicate active
`person_id`. **Warning** severity (recorded, never blocks service): a filename
that no longer matches its front-matter `id`, and a `related_person_id` that does
not resolve to a known active person (it may legitimately point at an archived
person). The report is written to `<data_dir>/validation-report.json` on every
startup and every `validate_repository` / `montauk validate`.

## 28. Error Reporting and Logs

- Generate a fresh validation-report.json (or equivalent) on every startup.

- Maintain ordinary rotating application logs for historical troubleshooting.

- Logs may record timestamps, agent ID, tool name, status, latency, and affected person ID.

- Do not log sensitive fact text, full person records, contact details, or raw request payloads by default.

- Expose current validation errors through MCP so an assistant can diagnose why a person is missing.

## 29. Git History

Git is an audit/recovery mechanism, not part of the logical database schema.

- Once per day, a maintenance job checks whether canonical Markdown data changed since the last commit.

- If changed, create one local Git commit; if unchanged, do nothing.

- At most one scheduled automatic commit per day.

- Daily commit time is configurable.

- Do not automatically push.

- Do not commit derived SQLite/vector indexes or secrets.

- Manual and MCP-originated edits are both captured by the next snapshot.

- Semantically meaningful history remains in current person records; misinformation/edit mistakes are corrected and recoverable through Git if needed.

**As built.** The git repository is **inside the deployment's `data_dir`**, wholly
separate from the source-code repo (a real `data_dir` is typically outside the
source tree). `montauk init` / `ensure_git_repo` create it on branch `main` with
a `.gitignore` excluding `index/`, `auth/`, `logs/`, `validation-report.json`,
`.montauk.lock`, and the migration map/backups. Only the canonical pathspecs
(`people/`, `archive/`, `person-id-sequence.json`) are ever staged — never
`git add -A`. Automated commits use a distinct identity (`Montauk
<montauk@localhost>`), applied per-invocation with `-c`, so no global git config
is written. `snapshot_if_changed` checks status scoped to the canonical paths and
commits at most once; it is idempotent across the in-process
`DailySnapshotScheduler` (asyncio task, configurable time) and any external cron
entry landing the same day. Pushing to a remote is entirely the operator's job.

## 30. Security and Access Control

Security is a Phase 1 requirement because the repository may contain highly sensitive personal information.

- Local mode binds to loopback only by default.

- Remote mode requires authenticated encrypted transport (HTTPS/TLS directly or through a documented secure reverse-proxy/private-network deployment).

- Never expose an unauthenticated server on 0.0.0.0.

- Each agent gets an individually identifiable credential and stable agent_id.

- Two authorization roles: read_only and read_write.

- Secrets are supplied via environment variables or external secret management, never YAML or Markdown.

- The Markdown data directory must not be served as static web content.

- Credentials should be revocable through the admin CLI.

- Audit logs identify the calling agent without logging sensitive content.

- Rate-limit or otherwise mitigate repeated failed remote authentication attempts.

- Public repository examples and tests must use synthetic data only.

**As built.**

- **Credential store:** `<data_dir>/auth/credentials.sqlite`, owned exclusively by
  `auth.CredentialStore` and never touched by the derived-index code or the git
  snapshot. Tokens are `mtk_<43 url-safe chars>`, stored only as SHA-256 hashes
  plus a 12-char prefix; the raw token is shown once by `montauk agents create`
  and is not recoverable. `agent_id` is the name slugified, with a `-2`, `-3`, ...
  suffix on collision. Roles: `read_only`, `read_write`. Revocation sets
  `revoked_at` (rows are kept for audit).
- **stdio auth:** one client, so identity is resolved once at startup from the
  `MONTAUK_AGENT_TOKEN` environment variable. If unset/invalid, mutation tools
  fail `PERMISSION_DENIED`; reads still work.
- **HTTP auth:** two layers. `BearerAuthMiddleware` rejects any request without a
  valid, non-revoked credential with `401` before MCP dispatch — so an
  unauthenticated server is never exposed, regardless of bind address. Per-call
  `_authorize_write` then re-resolves the same header and requires `read_write`
  for every mutation tool.
- **Transport encryption:** Montauk speaks **plain HTTP only**. Remote deployments
  put a TLS-terminating reverse proxy (e.g. Caddy) in front and keep
  `host: 127.0.0.1`. Binding `0.0.0.0`/`::` logs a plaintext-exposure warning but
  is not blocked (auth is still enforced).
- **`transport.public_url`** (added after the initial ID/scoping revisions): the
  external URL agents connect to through the proxy. When set, its hostname is
  added to the streamable-HTTP transport's Host-header allowlist so the proxy can
  `reverse_proxy` without a `header_up Host` rewrite, and DNS-rebinding protection
  is enabled for that host. When unset, DNS-rebinding protection is disabled for
  the remote transport (every request is still bearer-authenticated).
- **Not implemented in Phase 1:** rate-limiting / lockout for repeated failed
  authentication, and structured per-call audit log lines (agent, tool, status,
  latency). Logging is deliberately minimal (section 28); only interaction
  corrections emit an explicit info line. Both are Phase 2 candidates.

## 31. Configuration

Use YAML for non-secret configuration and environment variables for secrets. The
config is a strict Pydantic model (`config.py`, `extra="forbid"` — unknown keys
are rejected). All sections have defaults; a bare `data_dir` is a valid config.

**As built** (full schema, showing defaults):

```yaml
data_dir: ./data

transport:
  mode: stdio                 # "stdio" | "remote"
  host: 127.0.0.1
  port: 8765
  # public_url: https://montauk.example.com   # remote-behind-proxy only

git:
  enabled: true
  daily_snapshot_time: "03:00"

search:
  semantic_enabled: true
  max_candidates: 5
  similarity_threshold: 0.35  # calibrated for all-MiniLM-L6-v2

retrieval:                    # prepare_person_context (section 21.1)
  brief_tokens: 750
  standard_tokens: 2000
  comprehensive_tokens: 6000
  min_tokens: 100
  max_tokens: 8000
  lexical_enabled: true
  semantic_enabled: true
  interaction_chunk_tokens: 120
  interaction_chunk_overlap_tokens: 24

embedding:
  provider: local             # only "local" is accepted
  model: sentence-transformers/all-MiniLM-L6-v2

logging:
  level: INFO
  retention_days: 30
```

Notes: `search.semantic_enabled` and `retrieval.semantic_enabled` are separate
toggles (identity search vs. context retrieval). `retrieval` bounds are validated
for coherence (`min < max`, presets within `[min, max]`, overlap < chunk size).
`montauk init` writes a starter `config.yaml` with these values into the data
directory. Secrets are never read from config or environment beyond
`MONTAUK_AGENT_TOKEN` (stdio bearer token).

## 32. Administrative CLI

Provide a small administration CLI. It is not a person-record editor.

```text
montauk init --data-dir PATH              # scaffold a deployment + private local git repo
montauk serve                             # run the MCP server (the only long-running command)
montauk validate
montauk status
montauk migrate-ids                       # one-time cutover: name-derived IDs -> generic P0001 IDs
montauk rebuild-index                     # rebuild both derived indexes; --relational-only / --semantic-only / --model
montauk index-status                      # index versions, provider/model, counts, stale/orphaned entries, retrieval mode
montauk git-snapshot
montauk agents list
montauk agents create --role read_only --name briefing-agent
montauk agents revoke <agent-id>
montauk config-check --config PATH
```

Every command takes `--data-dir PATH` (quick/direct use) and/or `--config PATH`
(full YAML config); `--data-dir` overrides the config's `data_dir` when both are
given. `config-check` requires `--config`. The CLI is built with Typer;
`montauk` with no args prints help.

**`montauk init`** (added during the build) creates `people/`, `archive/`, the
`person-id-sequence.json` high-water mark, a starter `config.yaml`, a private-repo
`README.md`, and a local git repo on branch `main` with an initial commit and a
`.gitignore` excluding derived/secret paths. It is idempotent and never clobbers
an existing `config.yaml`. It never adds a git remote or pushes.

**`montauk serve`** loads config + logging, builds the context, runs startup
reconciliation, resolves the stdio identity (stdio mode), starts the daily git
snapshot task (if `git.enabled`), and runs the stdio or streamable-HTTP
transport. `--transport stdio|remote` overrides `transport.mode`.

Person content is changed through MCP operations or direct Markdown editing.

`montauk rebuild-index` delete-and-rebuilds the derived SQLite (relational) and semantic (vector) indexes from canonical Markdown -- both by default, or one via `--relational-only` / `--semantic-only`. It is safe to rerun, removes orphaned entries, does not corrupt a usable index if a rebuild fails, and prints records processed / entries written / provider+model / elapsed time -- never personal content. (`montauk rebuild-vectors` remains as a hidden deprecated alias for `--semantic-only`.) `montauk index-status` reports index health without personal content.

`montauk migrate-ids` converts an existing deployment whose person IDs are name-derived slugs to generic sequential IDs (`P0001`, ...). It is idempotent, validates the source before writing, takes a pre-migration git snapshot, renames every person file, rewrites every `related_person_id` reference, advances the ID allocator past every migrated ID, verifies that only `^P[0-9]{4,}$` IDs remain with matching filenames and no dangling references, and rebuilds the SQLite index. There is no legacy-ID resolver, `legacy_ids` field, or compatibility period: after the cutover, name-derived IDs fail normal validation like any other malformed ID.

## 33. Packaging and Deployment

- Publish the Python project distribution as montauk-mcp, use montauk as the import namespace, and provide montauk as the console-script entry point in pyproject.toml.

- Official Dockerfile and documented Docker deployment path.

- Document local stdio/local MCP usage where supported by the selected MCP SDK.

- Document remote authenticated transport separately.

- Keep persistent data mounted outside the container image.

- Provide config.example.yaml and .env.example containing names only, never real secrets.

- Pin or constrain dependencies appropriately and document supported Python versions.

**As built.**

- Distribution `montauk-mcp`, import package `montauk` (`src/` layout, hatchling
  build backend), console script `montauk = "montauk.cli:app"`.
- **Python 3.11+.** CI (`.github/workflows/ci.yml`) runs the suite on 3.11, 3.12,
  3.13 with `uv`. The Dockerfile builds on `python:3.14-slim`.
- Runtime dependencies: `mcp>=2.1,<3`, `pydantic>=2.12`, `pyyaml>=6.0`,
  `fastembed>=0.8.0`, `numpy>=2.0`, `typer>=0.15`, `starlette>=0.38`,
  `uvicorn>=0.30` (plus `tzdata` on Windows). Locked in `uv.lock`. Dev group:
  `pytest`, `pytest-asyncio`.
- Multi-stage Dockerfile: the builder stage `uv sync`s and pre-downloads the
  embedding model into `/opt/fastembed_cache`; the runtime stage is slim, runs as
  non-root uid 1000, and mounts `/data` as a volume. `ENTRYPOINT ["montauk"]`,
  default `CMD ["serve", "--data-dir", "/data"]`.
- `config/config.example.yaml` and `.env.example` ship with placeholder values
  only. `LICENSE` is MIT.

## 34. Testing Strategy

Ship static synthetic fixtures (recognizable fictional characters, never real
people). **As built there are two:**

- `examples/simpsons/` — identity, indexing, and edge-case coverage: a
  duplicate-name collision (P0002 / P0003, both "Gil Gunderson"), a
  missing-birth-year birthday, a person with a contact cadence but no recorded
  interactions, an archived person (P0009, Frank Grimes), and one deliberately
  malformed file (P0001, unterminated YAML string) to exercise degraded-health
  handling.
- `examples/dating/` — `prepare_person_context` coverage: a dating contact
  (P0001) and a mutual friend (P0002), with broad / exact-detail / temporal /
  advisory-shaped questions, an interaction, a `related_person_id` reference, and
  deliberate gaps (dogs but no dog name; birthday month but no day).

Automated tests should cover:

- Markdown parsing and round-trip preservation.

- Person-ID generation (generic sequential IDs, concurrency-safe allocation, never reused) and advisory `possible_duplicates` detection for shared names.

- Fixed-category fact operations.

- Approximate date validation.

- Confidence handling.

- Interaction insertion and last-contact derivation.

- Birthday queries with and without birth year.

- Cadence/overdue queries.

- Archive move and index removal.

- Startup reconciliation after direct Markdown edits.

- Malformed-file skipping and degraded health.

- SQLite rebuild from scratch.

- Vector-index rebuild from scratch.

- Semantic identity search returning evidence and multiple candidates.

- Read-only vs read-write authorization.

- Serialized concurrent writes.

- One-person batch atomicity and rollback on validation failure.

- Daily Git snapshot: commit only when data changed.

- No sensitive content in default logs.

- MCP discovery and tool-catalog behavior: server instructions are exposed where supported; tool descriptions/schemas are complete; and an integration test can discover the server without private agent-specific setup.

## 35. Recommended Internal Modules

Keep the integration contract close to server registration code so server instructions, tool descriptions, and JSON schemas are reviewed and tested as part of the public API.

**As built** (`src/montauk/`):

```text
server.py           # MCPServer construction + SERVER_INSTRUCTIONS
tools_core.py       # identity/read/write tools + MontaukContext + batch tool
tools_ops.py        # birthday/cadence/archive/validation/health tools
tool_types.py       # MCP request/response Pydantic models (Appendix A shapes)
models.py           # typed domain models (Person/Fact/Interaction/Source/ContactInfo)
markdown_store.py   # canonical parse/serialize, atomic write, PersonIdSequence
schema.py           # fixed categories + confidence enum
ids.py              # person/fact/interaction id format + alias normalization
dates.py            # FlexDate (variable precision) + Birthday
sqlite_index.py     # derived relational index + reconcile/rebuild
semantic_index.py   # chunking + numpy vector store + drift detection
embeddings/base.py  # EmbeddingProvider Protocol
embeddings/local.py # fastembed local provider (default)
search is in tools_core.search_people; hybrid context retrieval is person_context.py
person_context.py   # prepare_person_context: purpose analysis, hybrid ranking, selection
tokens.py           # conservative token estimate + sentence splitting
write_queue.py      # asyncio.Lock + fcntl.flock serialized writes
auth.py             # CredentialStore, bearer-token resolution, role check
http_app.py         # Starlette wrapper: BearerAuthMiddleware, DNS-rebind settings
reconciliation.py   # startup scan + validation report (health lives here + tools_ops)
git_snapshot.py     # data-dir-local git repo + daily snapshot scheduler
migration.py        # one-time name-derived -> generic ID cutover
config.py           # strict YAML config model
bootstrap.py        # assemble MontaukContext + run startup reconciliation
logging_config.py   # rotating file + stderr handlers
errors.py           # typed ToolError subclasses
cli.py              # Typer admin CLI (incl. `init` and `serve`)
```

There is no separate `search.py` or `health.py`: identity search is a tool in
`tools_core.py`, health/validation reporting lives in `reconciliation.py` and
`tools_ops.py`.

## 36. Recommended Implementation Sequence

1. Define typed domain models, fixed categories, date parser, and Markdown schema.

2. Implement MarkdownStore with create/read/narrow mutation/archive and atomic file replacement.

3. Implement validation and repository-wide startup scan.

4. Implement SQLite derived index and full rebuild/reconciliation.

5. Define the Agent Integration Contract: server instructions, tool descriptions, schemas, and ambiguity behavior.

6. Implement core exact MCP reads/writes with serialized write queue.

7. Implement one-person batch transaction semantics.

8. Add birthdays, cadence, archive, validation-errors, and health tools.

9. Add embedding-provider interface, local default provider, chunking, and semantic index.

10. Implement hybrid search_people with ranked candidates and evidence.

11. Add per-agent authentication and read_only/read_write authorization.

12. Add Git daily snapshot job.

13. Add admin CLI.

14. Add Docker packaging and deployment documentation.

15. Build the Simpsons fixture and comprehensive automated tests, including agent-discovery/tool-selection tests.

16. Security review, logging review, failure-injection tests, protocol-conformance checks, and public-repo cleanup.

## 37. Acceptance Criteria for Phase 1

- A fresh deployment can create a person, add facts/interactions, restart, and recover identical canonical data.

- Deleting SQLite/vector derived data and rebuilding produces a working equivalent index.

- Two people with the same name can coexist and be reliably distinguished by short person IDs.

- A vague natural-language search can return plausible candidates with evidence without the MCP server making an irreversible identity decision.

- An external agent can request either a narrow subset of information or the complete person record.

- Manual Markdown edits become active after restart/reconciliation.

- One malformed person file does not prevent healthy records from being served.

- Concurrent agent writes do not corrupt files.

- Read-only agents cannot mutate data.

- Remote access can be deployed without exposing unauthenticated personal data.

- Archived people disappear from normal indexes and proactive relationship queries.

- Birthday and overdue-contact queries work deterministically.

- Automatic Git history creates no more than one scheduled commit per changed day and never auto-pushes.

- No Phase 1 feature requires an LLM API key.

- A newly connected MCP-capable agent can discover the available tools and intended interaction patterns without agent-specific hard-coded integration: it can infer search-before-create/write when identity is unknown, surface ambiguity rather than guess, prefer targeted retrieval for narrow questions, and use the atomic one-person batch operation for related updates.

- The Montauk server publishes concise Montauk usage instructions through the current MCP discovery mechanism where supported, while each critical tool also carries sufficient local description/schema guidance to remain usable when a host does not prominently expose server instructions.

- Tests verify that correctness-critical behavior is enforced deterministically even when a caller ignores or violates the natural-language integration instructions.

- New people receive immutable generic IDs (`P0001`, ...), allocated concurrency-safely and never reused; a person's name can be incomplete at creation and later corrected via `update_person_name` without changing identity or recreating the record; former/partial/misspelled names remain searchable as aliases.

- After `montauk migrate-ids`, every existing person and every persisted reference uses a generic ID, name-derived IDs are rejected by normal validation, and no legacy-ID compatibility schema or resolver remains.

- Agents can correct interaction details and participant attribution with `update_interaction`, and remove wholly erroneous or duplicate interactions with `remove_interaction`; a corrected former participant retains no active or searchable trace, while accurate history is protected from removal by server instructions and tool descriptions.

- `prepare_person_context(person_id, purpose, detail_level, max_tokens)` returns a token-bounded, metadata-reduced evidence packet of the facts, relationships, and interactions from one person's record relevant to the purpose, selected with hybrid lexical + semantic retrieval (lexical-only when semantic is unavailable, and it says so); exact terms/quotations/names/places/dates do not depend on vector similarity alone; retrieval never crosses people; canonical Markdown stays authoritative and the index is synchronized after every mutation, validated and rebuildable, and never serves known-stale semantic results; no hosted embedding provider receives personal data by default; truncation and additional matching content are reported honestly; and Montauk generates no advice, judgments, recommendations, quotations, or missing facts -- broad, exact, temporal, and advisory dating-contact fixtures demonstrate this.

## 38. Phase 2 Opportunities

- Server-side LLM ingestion of raw WhatsApp/email/meeting material.

- Automatic fact extraction with confidence assignment.

- Identity resolution and alias reconciliation with human confirmation for ambiguous cases.

- Server-side summarization and briefing generation.

- Profile photos and other attachments.

- Defined semantics for interaction connection_level and possible cadence weighting.

- Optional richer relationship graph features.

- Additional database backends such as PostgreSQL.

- More sophisticated permissions/delegation if multi-agent use requires them.

- Optional notification integrations, while keeping scheduling concerns separable from canonical memory.

## 39. Key Design Principles

- Human-readable first: the owner can inspect and edit the real database with a text editor.

- One source of truth: canonical facts live in Markdown; indexes are disposable.

- Intelligence outside the store: Phase 1 records what agents tell it rather than trying to interpret the world itself.

- Small, deterministic server responsibilities: validate, store, index, search, retrieve, authorize, and preserve.

- Ambiguity is surfaced, not hidden: candidate lists are preferable to confident misidentification.

- Security by default: private data is never intentionally exposed merely for deployment convenience.

- Avoid schema cleverness: add structure only where it enables a concrete deterministic use case.

- Context-efficient retrieval: return only what an agent needs unless it explicitly asks for everything.

- Manual editing is a feature, not an escape hatch.

## 40. Build Decisions and Deviations

Consolidated list of the decisions made during implementation that a Phase 2
spec should build on. Each is expanded in the section noted.

**Committed as spec amendments during the build** (already integrated above):

- **Generic person IDs + mutable names + interaction corrections** (sections 7,
  9, 12.1, 23, 32; commit `04b4a4e`). Person IDs became opaque sequential
  `P0001…` with a git-tracked high-water-mark allocator; names are mutable via
  `update_person_name`; `update_interaction` (incl. `move_to_person_id`) and
  `remove_interaction` were added; `montauk migrate-ids` was added for the
  one-time cutover from name-derived IDs.
- **Record scoping** (section 15.1; commit `f340ff3`). "A source may be about
  several people; a record is about exactly one." Enforced structurally
  (one-person batches, `related_person_id` must resolve) and via `RECORD SCOPING`
  server + tool text.
- **`prepare_person_context`** (sections 21.1, 24; commit `2de9869`). New
  purpose-specific hybrid-retrieval evidence-packet tool with `brief` / `standard`
  / `comprehensive` budgets, and a `retrieval:` config block.

**Decided after those amendments / still only in code until this revision:**

- **`transport.public_url`** and remote DNS-rebinding behavior (sections 30, 31;
  commit `e6a7a64`).
- **`montauk init`** deployment + private-git-repo scaffolder, and **`montauk
  serve`** as the single long-running command (sections 5, 6, 32; commit
  `b897396`).
- **Embedding stack:** `fastembed` + `all-MiniLM-L6-v2`; **vector store:**
  brute-force numpy, no ANN library (sections 19, 20, Appendix C).
- **`search.similarity_threshold` default lowered 0.55 → 0.35** for the chosen
  model (sections 19, 31).
- **Two fixtures** (`examples/simpsons/`, `examples/dating/`) rather than one
  (section 34).
- Canonical Markdown is **regenerated whole on every write**; no
  formatting-preservation layer (section 8, Appendix C).
- Validation distinguishes **error vs warning** severity; filename/id mismatch
  and dangling `related_person_id` are warnings that never block startup
  (section 27).

**Listed as Phase 1 requirements but deferred to Phase 2:**

- **Rate-limiting / lockout** on repeated failed remote authentication
  (section 30).
- **Structured per-call audit log lines** (agent, tool, status, latency). Logging
  is minimal by design; only interaction corrections emit an explicit line
  (sections 28, 30).
- **MCP prompts** — not implemented; not needed for autonomous agent use
  (sections 24, 25.1).
- **Hosted embedding provider adapter** — the interface exists but
  `embedding.provider` is `Literal["local"]`; enabling a hosted provider is a
  config-schema change, not just code (section 20).
- **`AMBIGUOUS` / `INDEX_DEGRADED` error codes** are defined but never raised;
  the conditions are reported through return values instead (Appendix A).

## Appendix A. Recommended API Semantics

Mutation tools should return compact machine-friendly results including person_id, changed object IDs, validation status, and index-update status. `create_person` and `update_person_name` additionally return `possible_duplicates` (other records sharing the name) and `warnings`; the interaction-correction tools return the affected person IDs and, for a re-attribution, the new interaction id on the corrected person. Search tools should return concise evidence, not entire records unless explicitly requested. Errors should be typed and actionable: NOT_FOUND, AMBIGUOUS, VALIDATION_ERROR, PERMISSION_DENIED, ARCHIVED, INDEX_DEGRADED, and INTERNAL_ERROR are reasonable starting classes.

**As built.** All seven codes are defined (`errors.py`), each a `ToolError`
subclass whose message is prefixed with `CODE: `. Actively raised: `NOT_FOUND`,
`VALIDATION_ERROR`, `PERMISSION_DENIED`, `ARCHIVED`, and `INTERNAL_ERROR` (the
base). `AMBIGUOUS` is not raised — `search_people` returns the candidate list
instead. `INDEX_DEGRADED` is not raised — a derived-index failure surfaces as
`index_update_status: "degraded"` on the write result (canonical Markdown still
committed, per Appendix B), and semantic-index staleness surfaces as
`retrieval.semantic_available: false` with a reason. Mutation results are
`WriteResult` / `NameUpdateResult` / `InteractionMutationResult` (`tool_types.py`)
carrying `person_id`, `changed_ids`, `index_update_status`, and — for
create/rename — `possible_duplicates` and `warnings`.

## Appendix B. Atomic Markdown Writes

For safety, never edit canonical person files in place. Build and validate the complete new representation in memory, write to a temporary file in the same filesystem, fsync as appropriate, and atomically replace the destination. Only after canonical replacement succeeds should derived indexes be updated. If derived-index update fails, mark health degraded and allow reconciliation to repair it; never roll canonical Markdown backward merely because a disposable index failed.

## Appendix C. Open Technical Choices Delegated to Implementation

- Exact official Python MCP SDK transport APIs and server framework conventions.

- Exact local embedding model.

- Exact vector index backend (embedded/local preferred for Phase 1).

- Exact authentication mechanism compatible with the selected MCP remote transport, while preserving individual agent identity and two roles.

- Exact YAML library, typed-model library, scheduler, and logging packages.

- Exact Markdown serialization library and formatting-preservation strategy.

- Exact default similarity threshold and result limit.

- Exact numerical meaning of connection_level remains intentionally undefined.

These are implementation details, not unresolved product requirements. Choose mature, lightweight Python components and keep replaceable interfaces around embedding, vector storage, authentication, and indexing.

**As resolved in Phase 1:**

| Choice | Resolution |
| --- | --- |
| MCP SDK / transport | `mcp` package (`>=2.1,<3`), `MCPServer`; `run_stdio_async` and `streamable_http_app` (endpoint `/mcp`) behind Starlette + uvicorn |
| Local embedding model | `sentence-transformers/all-MiniLM-L6-v2` (384-dim) via `fastembed` (ONNX Runtime, no torch) |
| Vector index backend | Brute-force cosine over a persisted `float32` numpy matrix + `chunks.sqlite` metadata; no ANN library |
| Auth mechanism | Opaque bearer tokens (`mtk_…`), SHA-256-hashed in `auth/credentials.sqlite`; HTTP `Authorization` header per request, `MONTAUK_AGENT_TOKEN` env var for stdio |
| YAML / models / scheduler / logging | `pyyaml`; Pydantic v2; in-process `asyncio` daily-snapshot task; stdlib `logging` with `TimedRotatingFileHandler` |
| Markdown serialization | Hand-rolled: `pyyaml` front matter + fixed headings; whole-file canonical regeneration on every write (no in-place edit, no formatting-preservation library) |
| Similarity threshold / result limit | `0.35` / `5` (config: `search.similarity_threshold`, `search.max_candidates`) |
| `connection_level` semantics | Still intentionally undefined; validated as an integer 1–6 |
| Relational backend | SQLite (WAL); storage kept behind `SqliteIndex` so Postgres remains a later option |
