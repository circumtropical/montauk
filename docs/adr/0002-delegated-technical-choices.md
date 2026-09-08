# ADR 0002 — Delegated technical choices (Appendix C)

Date: 2026-09-08
Status: Accepted (items in scope for the database/migration/dashboard increment)

Spec Appendix C delegates several choices to the implementation and requires
them to be recorded. The connector/messaging items (1, 2) are out of scope for
this increment and will get their own ADR. The rest:

## 3. Web framework and frontend approach

**Decision: FastAPI + server-rendered Jinja2 templates, progressive
enhancement with a little vanilla JS. No SPA, no Node build step.**

Rationale: reuses the existing Python/Pydantic codebase directly; the
dashboard is mostly forms and tables over the same domain models; server
rendering keeps the deployment a single Python image; keyboard accessibility
and mobile-responsive layout are straightforward with plain HTML/CSS. Sessions
via signed cookies (`itsdangerous`), CSRF via per-session token, auth rate
limiting in the service layer.

## 4. PostgreSQL migration library / ORM

**Decision: SQLAlchemy 2.0 (typed ORM) + Alembic migrations.**

Rationale: the de facto standard, mature on Python 3.14, integrates with
Pydantic-style typing, gives us transactions/constraints/connection pooling
without hand-rolling. Repositories wrap the ORM Session and are the only place
that builds queries, so workspace scoping is enforced in one layer.

## 5. Jobs / advisory-lock design and separate worker process

**Decision: no separate worker process and no job queue in this increment.**

The migrator is a CLI command; the dashboard is request/response. Serialized
writes use a PostgreSQL transaction plus a per-workspace advisory lock
(`pg_advisory_xact_lock`) for the person-ID allocator. A worker process and
durable `processing_jobs` arrive with extraction.

## 6. pgvector vs. retained local vector index

**Decision: retain the Phase 1 rebuildable local vector index
(`semantic_index.py`), rebuilt from PostgreSQL instead of from Markdown.**

Rationale: spec §24.1 explicitly permits this; avoids requiring the `pgvector`
extension in the container image now; preserves lexical-only degradation, model
fingerprinting, and rebuildability unchanged. Revisit if transcript search at
scale (a later increment) makes a single store compelling.

## 7. Secret-encryption library and key-rotation envelope

**Decision: application-level AES-256-GCM via `cryptography`'s `AESGCM`, keyed
by a master key from `MONTAUK_MASTER_KEY` (env or mounted file), wrapped behind
a `SecretBox` abstraction.** No encrypted secrets are actually stored in this
increment (no connector/LLM credentials yet), but owner password hashes
(Argon2id, `argon2-cffi`) and agent-token hashes (SHA-256, unchanged from
Phase 1) are. `SecretBox` is written now so the connector increment has a
single place to evolve toward per-workspace keys.

## 8. MCP OAuth / authorization protocol integration

**Deferred** to the Phase 2 MCP surface increment. The dashboard's own
password/session auth is independent of it.
