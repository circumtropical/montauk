# Montauk

Relationship memory for personal agents. An MCP server giving a personal-assistant agent
durable, private, human-readable memory of the people in one human user's life.

The canonical database is a directory of Markdown person files (`data/people/*.md`). SQLite
and a semantic vector index are derived, fully disposable acceleration layers that can be
deleted and rebuilt at any time without data loss. See `montauk_phase1_build_spec.md` for the
full Phase 1 specification this implementation follows.

**Identity vs. name.** Each person has a permanent, generic ID (`P0001`, `P0002`, ...) that
Montauk assigns and never changes or reuses. The person's `name` is just the best label
currently known — it can be partial or wrong at first and corrected later with
`update_person_name` (former/alternate spellings are kept as searchable aliases), all without
touching the ID or the filename (`data/people/P0001.md`). A deployment still on the old
name-derived IDs (`mike-chen`) converts once with `montauk migrate-ids`.

**Purpose-specific retrieval.** `prepare_person_context(person_id, purpose, detail_level, max_tokens)`
returns a token-bounded evidence packet — the facts, relationships, and interactions from one
person's record relevant to a stated question — selected with hybrid lexical (BM25) + semantic
retrieval and stripped of storage metadata. It is evidence, not an answer: Montauk never
generates advice, quotations, or missing facts. Semantic search runs locally by default (no
data leaves the machine) and degrades to lexical-only, transparently, when unavailable.

## Quick start (local, stdio)

```bash
uv sync
uv run montauk init --data-dir ~/.local/share/montauk
uv run montauk agents create --name my-agent --role read_write --data-dir ~/.local/share/montauk
# prints a token once -- save it
export MONTAUK_AGENT_TOKEN=<the token printed above>
uv run montauk serve --config ~/.local/share/montauk/config.yaml
```

`serve` reads `MONTAUK_AGENT_TOKEN` once at startup to resolve the single stdio client's
identity (there is exactly one client on stdio, so there's no per-request header to read).
Point an MCP-compatible agent host at that `serve` command as a local stdio server. Try it
against the bundled Simpsons fixture instead of an empty repo by pointing `--data-dir` at
`examples/simpsons/`.

## Setting up your private data repository

`montauk init` scaffolds a deployment: it creates the data directory (`people/`, `archive/`),
writes a starter `config.yaml`, and initialises a **local** git repository for the canonical
Markdown -- on branch `main`, with an initial commit, and a `.gitignore` that excludes the
derived indexes (`index/`), agent credentials (`auth/`), and logs.

```bash
uv run montauk init --data-dir ~/.local/share/montauk
```

Choose a data directory **outside** this source checkout -- `~/.local/share/montauk` for a
personal deployment, `/var/lib/montauk` for a system service. Never put it inside a synced
folder (Dropbox/iCloud/Drive): sync races corrupt the git repo and it copies plaintext personal
data to a third party.

This data repository holds real personal information, so keep it **private and separate** from
the Montauk source code. Montauk deliberately never adds a git remote and never pushes (spec
§29) -- that is yours to wire up:

```bash
cd ~/.local/share/montauk
# create an EMPTY private repo on your git host first (no README / license / .gitignore)
git remote add origin git@github.com:<you>/<your-data-repo>.git
git push -u origin main
```

While `montauk serve` runs it makes at most one local commit per day, when a person file has
changed. Pushing those commits to your remote is up to you -- e.g. a cron entry:

```
15 3 * * *  cd ~/.local/share/montauk && git push -q origin main
```

`montauk init` is idempotent and never clobbers an existing `config.yaml`, so it is safe to
re-run.

## Configuration

Non-secret settings live in a YAML config file; copy `config/config.example.yaml` and adjust
`data_dir`, transport mode, git snapshot time, and search/embedding settings. Validate a config
without starting the server:

```bash
uv run montauk config-check --config config.yaml
```

Secrets are never read from config files -- agent credentials live in
`<data_dir>/auth/credentials.sqlite`, managed only via `montauk agents ...`. See `.env.example`
for the one environment variable the stdio transport reads (`MONTAUK_AGENT_TOKEN`).

## Admin CLI

```
montauk init --data-dir PATH              # scaffold a new deployment + private data repo
montauk validate                          # scan + validate; exit 1 if unhealthy
montauk status                            # health summary
montauk migrate-ids                       # one-time: convert name-derived IDs to generic P0001 IDs
montauk rebuild-index                     # rebuild both derived indexes (--relational-only / --semantic-only)
montauk index-status                      # derived-index health + retrieval mode (no personal content)
montauk git-snapshot                      # commit data/people + data/archive now, if changed
montauk agents list
montauk agents create --name X --role read_only|read_write
montauk agents revoke <agent-id>
montauk config-check --config config.yaml
montauk serve                             # the only long-running command
```

Every command accepts `--data-dir PATH` for quick/direct use, or `--config PATH` to load a full
YAML config (data-dir overrides the config's `data_dir` if both are given). Person *content* is
never edited through this CLI -- only through MCP tools or by hand-editing the Markdown directly
(changes take effect after the next server restart or `rebuild-index`).

## Remote (authenticated HTTPS) deployment

Set `transport.mode: remote` in the config and keep `host: 127.0.0.1` -- Montauk only speaks
plain HTTP, so a TLS-terminating reverse proxy on the same host handles the internet-facing side.
Each remote agent authenticates with `Authorization: Bearer <token>` from `montauk agents create`.

Also set `transport.public_url` to the URL agents connect to, e.g.:

```yaml
transport:
  mode: remote
  host: 127.0.0.1
  port: 8765
  public_url: https://montauk.example.com
```

`public_url` adds that hostname to the transport's Host-header allowlist, so the proxy can
forward requests unchanged -- a plain `reverse_proxy 127.0.0.1:8765` is enough, with no
`header_up Host` rewrite. The MCP endpoint is served at path `/mcp`, so agents connect to
`https://montauk.example.com/mcp`. A minimal Caddy site:

```caddy
montauk.example.com {
	reverse_proxy 127.0.0.1:8765
}
```

If `public_url` is left unset, DNS-rebinding protection is disabled for the remote transport
(every request is still bearer-authenticated); binding `0.0.0.0`/`::` additionally logs a
plaintext-exposure warning.

## Docker

```bash
docker build -t montauk-mcp .
docker volume create montauk-data
docker run --rm -v montauk-data:/data montauk-mcp agents create --name my-agent --role read_write --data-dir /data
docker run --rm -i -v montauk-data:/data -e MONTAUK_AGENT_TOKEN=<token> montauk-mcp serve --data-dir /data
```

The image pre-downloads the default embedding model at build time, so a fresh container needs
no network access on first run. Persistent data (`/data`) is a volume, never baked into the
image.

**Bind mounts and file ownership:** the container runs as a non-root user (uid 1000). A Docker
*named volume* (as above) works out of the box -- Docker initializes it from the image's `/data`
ownership on first use. A host *bind mount* (`-v /path/on/host:/data`) does not: the host
directory's existing ownership wins, so if it isn't already owned by uid 1000, `montauk` won't be
able to write to it. Either `chown -R 1000:1000` the host directory first, or run the container
with `--user "$(id -u):$(id -g)"` to match your host user instead.

## Phase 2 (in progress): PostgreSQL canonical store + web dashboard

Phase 2 moves the canonical store from Markdown to PostgreSQL and adds a web
dashboard. This is being built in increments (see `montauk_phase2_build_and_migration_spec.md`
and `docs/adr/`). **Landed so far:** the PostgreSQL schema, the Phase 1 → PostgreSQL
migrator, and a read-focused dashboard.

```bash
export MONTAUK_DATABASE_URL=postgresql://user:pass@localhost:5432/montauk
uv run montauk db upgrade                       # create / update the schema
uv run montauk dashboard                        # http://127.0.0.1:8817  (first visit = setup wizard)
```

Then, with the dashboard's owner/workspace created, import an existing Phase 1
deployment:

```bash
uv run montauk migrate phase2 --source-data-dir ~/.local/share/montauk --workspace "Personal"
# review the report, then:
uv run montauk migrate phase2 --source-data-dir ~/.local/share/montauk --workspace "Personal" --execute
```

The migrator is dry-run by default, backs the Phase 1 tree up by *copying* it
(the tree and its Git history are never modified), stages the whole import in one
transaction, preserves every permanent person ID, and verifies each record by
comparing canonical-Markdown hashes before committing. Malformed Phase 1 files
block an `--execute` run rather than being skipped.

**Not in this increment:** the source connectors (WhatsApp, Google Messages,
Gmail, IMAP), the LLM extraction/review pipeline, and encrypted backups. Until
those land, the Phase 1 stdio/HTTP MCP server (`montauk serve`, below) still runs
against the Markdown tree unchanged; port it to PostgreSQL is the next increment.

## Development

```bash
uv sync
uv run pytest            # needs Docker (spins a postgres testcontainer) or MONTAUK_TEST_DATABASE_URL
uv run ruff check . && uv run mypy
```

`examples/simpsons/` is a synthetic fixture (recognizable fictional characters, not real people)
used by the test suite and handy for manual exploration -- it includes a duplicate-name
collision, a missing-birth-year birthday, a person with a contact cadence but no recorded
interactions, an archived person, and one deliberately malformed file, exercising the
corresponding edge cases end to end.

`examples/dating/` is a second synthetic fixture (a fictional dating contact and a mutual
friend) used to exercise `prepare_person_context`: broad, exact-detail, temporal, and advisory
questions, an interaction, a relationship reference, and deliberate gaps (dogs but no dog name,
a birthday month but no day).
