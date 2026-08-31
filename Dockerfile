# syntax=docker/dockerfile:1
#
# Multi-stage build: `builder` resolves/installs dependencies with uv and
# pre-downloads the default embedding model's weights, so a freshly
# started container needs no network access on first run. `runtime` is
# the slim image that actually ships.

FROM python:3.14-slim AS builder

RUN pip install --no-cache-dir uv

# Built at the same absolute path (/app) the runtime stage copies it to:
# a uv-managed venv's scripts get a shebang hardcoding this build path
# (e.g. #!/app/.venv/bin/python), so a path mismatch between stages
# leaves every console-script entry point broken at runtime.
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --frozen --no-dev --no-editable

# Pinned away from fastembed's default (a /tmp-based path some container
# runtimes wipe on start) so the pre-downloaded weights are guaranteed
# to still be there at runtime -- see embeddings/local.py.
ENV FASTEMBED_CACHE_PATH=/opt/fastembed_cache
RUN uv run --no-sync python -c \
    "from montauk.embeddings.local import LocalEmbeddingProvider; LocalEmbeddingProvider()"


FROM python:3.14-slim AS runtime

RUN useradd --create-home --uid 1000 montauk
WORKDIR /app

ENV FASTEMBED_CACHE_PATH=/opt/fastembed_cache
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /opt/fastembed_cache /opt/fastembed_cache
COPY config/config.example.yaml /app/config/config.example.yaml

ENV PATH="/app/.venv/bin:$PATH"

RUN mkdir -p /data \
    && chown -R montauk:montauk /data /opt/fastembed_cache

# Persistent data (people/, archive/, derived indexes, credentials)
# lives outside the image -- mount a volume here in real deployments.
VOLUME ["/data"]
EXPOSE 8765

USER montauk

ENTRYPOINT ["montauk"]
CMD ["serve", "--data-dir", "/data"]
