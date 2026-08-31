# Montauk

Relationship memory for personal agents. An MCP server giving a personal-assistant agent
durable, private, human-readable memory of the people in one human user's life.

The canonical database is a directory of Markdown person files (`data/people/*.md`). SQLite
and a semantic vector index are derived, fully disposable acceleration layers that can be
deleted and rebuilt at any time without data loss. See `montauk_phase1_build_spec.md` for the
full Phase 1 specification this implementation follows.

## Quick start (local, stdio)

```bash
uv sync
uv run montauk agents create --name my-agent --role read_write --data-dir ./data
# prints a token once -- save it
export MONTAUK_AGENT_TOKEN=<the token printed above>
uv run montauk serve --data-dir ./data
```

`serve` reads `MONTAUK_AGENT_TOKEN` once at startup to resolve the single stdio client's
identity (there is exactly one client on stdio, so there's no per-request header to read).
Point an MCP-compatible agent host at `uv run montauk serve --data-dir ./data` as a local stdio
server. Try it against the bundled Simpsons fixture instead of an empty repo by pointing
`--data-dir` at `examples/simpsons/`.

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
montauk validate                          # scan + validate; exit 1 if unhealthy
montauk status                            # health summary
montauk rebuild-index                     # rebuild the derived SQLite index from Markdown
montauk rebuild-vectors                   # rebuild the derived semantic index from Markdown
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

Set `transport.mode: remote` in the config. Montauk refuses to start a remote transport bound to
`0.0.0.0`/`::` unless auth is configured (it is, by default) -- put a TLS-terminating reverse
proxy in front for real internet-facing deployments; Montauk itself only speaks plain HTTP.
Each remote agent authenticates with `Authorization: Bearer <token>` from
`montauk agents create`.

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

## Development

```bash
uv sync
uv run pytest
```

`examples/simpsons/` is a synthetic fixture (recognizable fictional characters, not real people)
used by the test suite and handy for manual exploration -- it includes a duplicate-name
collision, a missing-birth-year birthday, a person with a contact cadence but no recorded
interactions, an archived person, and one deliberately malformed file, exercising the
corresponding edge cases end to end.
