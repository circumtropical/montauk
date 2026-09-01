"""Non-secret YAML configuration (spec section 31). Secrets (tokens,
credentials) are never read from here -- they live in data/auth/
credentials.sqlite, populated via the admin CLI, never via config files
or environment variables holding raw secret material.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .embeddings.local import DEFAULT_MODEL_NAME


class TransportConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["stdio", "remote"] = "stdio"
    host: str = "127.0.0.1"
    port: int = 8765
    # Public base URL agents use to reach a "remote" deployment through a
    # TLS-terminating reverse proxy, e.g. "https://montauk.example.com".
    # When set, the streamable-HTTP transport adds this hostname to its
    # Host-header allowlist, so the proxy can forward requests unchanged
    # (no `header_up Host` rewrite needed). Ignored when mode is "stdio".
    public_url: str | None = None

    @field_validator("public_url")
    @classmethod
    def _validate_public_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(
                'transport.public_url must be an http(s) URL with a hostname, e.g. "https://montauk.example.com"'
            )
        return value.rstrip("/")


class GitConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    daily_snapshot_time: str = "03:00"


class SearchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    semantic_enabled: bool = True
    max_candidates: int = 5
    # See tools_core.py's MontaukContext.similarity_threshold docstring:
    # empirically calibrated for the default local model, not the higher
    # value that might look natural from a generic "0-1 similarity" prior.
    similarity_threshold: float = 0.35


class EmbeddingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Only "local" runs by default. A hosted provider must be named here
    # explicitly AND enabled -- an API key in the environment is never on
    # its own sufficient authorization to upload relationship data.
    provider: Literal["local"] = "local"
    model: str = DEFAULT_MODEL_NAME


class RetrievalConfig(BaseModel):
    """Purpose-specific context retrieval (prepare_person_context)."""

    model_config = ConfigDict(extra="forbid")

    brief_tokens: int = 750
    standard_tokens: int = 2000
    comprehensive_tokens: int = 6000
    min_tokens: int = 100
    max_tokens: int = 8000
    lexical_enabled: bool = True
    semantic_enabled: bool = True
    # Interaction summaries longer than this (estimated tokens) are split
    # into overlapping chunks at sentence boundaries for indexing; shorter
    # ones stay whole. Facts and the person summary are never split.
    interaction_chunk_tokens: int = 120
    interaction_chunk_overlap_tokens: int = 24

    @field_validator("min_tokens", "max_tokens", "brief_tokens", "standard_tokens", "comprehensive_tokens")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("retrieval token budgets must be positive")
        return v

    @model_validator(mode="after")
    def _coherent_bounds(self) -> RetrievalConfig:
        if self.min_tokens >= self.max_tokens:
            raise ValueError("retrieval.min_tokens must be below retrieval.max_tokens")
        for name in ("brief_tokens", "standard_tokens", "comprehensive_tokens"):
            v = getattr(self, name)
            if not (self.min_tokens <= v <= self.max_tokens):
                raise ValueError(f"retrieval.{name} ({v}) must be within [min_tokens, max_tokens]")
        if self.interaction_chunk_overlap_tokens >= self.interaction_chunk_tokens:
            raise ValueError("retrieval.interaction_chunk_overlap_tokens must be below interaction_chunk_tokens")
        return self

    def budget_for(self, detail_level: str) -> int:
        return {
            "brief": self.brief_tokens,
            "standard": self.standard_tokens,
            "comprehensive": self.comprehensive_tokens,
        }[detail_level]

    def chunk_fingerprint(self) -> str:
        return f"iact:{self.interaction_chunk_tokens}/{self.interaction_chunk_overlap_tokens}"


class LoggingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: str = "INFO"
    retention_days: int = 30


class MontaukConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_dir: str = "./data"
    transport: TransportConfig = Field(default_factory=TransportConfig)
    git: GitConfig = Field(default_factory=GitConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @property
    def data_dir_path(self) -> Path:
        return Path(self.data_dir).expanduser().resolve()

    @property
    def daily_snapshot_time_parsed(self):
        import datetime as dt

        hour, _, minute = self.git.daily_snapshot_time.partition(":")
        return dt.time(int(hour), int(minute))


def load_config(path: Path | str) -> MontaukConfig:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"config file {path} must contain a YAML mapping at the top level")
    return MontaukConfig.model_validate(raw)
