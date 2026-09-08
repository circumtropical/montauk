"""Runtime context for the Phase 2 MCP server."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from ..config import RetrievalConfig
from ..db.crypto import SecretBox


@dataclass
class Mcp2Context:
    """Everything a Phase 2 MCP tool needs: a session factory for the
    canonical Postgres store, the master key box for decrypting model
    credentials (``None`` when ``MONTAUK_MASTER_KEY`` is unset -- briefings
    then fall back to deterministic evidence), and retrieval tuning.
    """

    session_factory: sessionmaker[Session]
    secret_box: SecretBox | None = None
    retrieval: RetrievalConfig | None = None
    max_candidates: int = 8

    @property
    def retrieval_config(self) -> RetrievalConfig:
        return self.retrieval or RetrievalConfig()
