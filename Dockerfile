# syntax=docker/dockerfile:1
#
# Multi-stage build. `builder` resolves dependencies with uv and
# pre-downloads the default embedding-model weights so the runtime image
# needs no network on first run. `runtime` is the slim image that ships.

FROM python:3.14-slim AS builder

RUN pip install --no-cache-dir uv

# Built at the same absolute path (/app) the runtime stage copies it to:
# a uv-managed venv's console scripts hardcode this path in their shebang.
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --frozen --no-dev --no-editable

# Pinned away from fastembed's /tmp default so the pre-downloaded weights
# survive a container restart (see embeddings/local.py).
ENV FASTEMBED_CACHE_PATH=/opt/fastembed_cache
RUN uv run --no-sync python -c \
    "from montauk.embeddings.local import LocalEmbeddingProvider; LocalEmbeddingProvider()"


FROM python:3.14-slim AS runtime

RUN useradd --create-home --uid 1000 montauk
WORKDIR /app

ENV FASTEMBED_CACHE_PATH=/opt/fastembed_cache
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /opt/fastembed_cache /opt/fastembed_cache

ENV PATH="/app/.venv/bin:$PATH"
RUN chown -R montauk:montauk /opt/fastembed_cache
USER montauk

# The canonical store is an external PostgreSQL database -- set
# MONTAUK_DATABASE_URL and MONTAUK_MASTER_KEY in the environment.
# `dashboard` (8817) and `mcp` (8766) are the two long-running commands.
EXPOSE 8817 8766
ENTRYPOINT ["montauk"]
CMD ["dashboard", "--host", "0.0.0.0"]
