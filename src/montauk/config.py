"""Non-secret YAML configuration (spec section 31). Secrets (tokens,
credentials) are never read from here -- they live in data/auth/
credentials.sqlite, populated via the admin CLI, never via config files
or environment variables holding raw secret material.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .embeddings.local import DEFAULT_MODEL_NAME


class TransportConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["stdio", "remote"] = "stdio"
    host: str = "127.0.0.1"
    port: int = 8765


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

    provider: Literal["local"] = "local"
    model: str = DEFAULT_MODEL_NAME


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
