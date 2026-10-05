"""Runtime context for the Phase 2 MCP server."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from ..config import RetrievalConfig
from ..db.crypto import SecretBox
from ..db.repositories import WorkspaceScope
from ..embeddings.base import EmbeddingProvider
from ..semantic_index import SemanticIndex


@dataclass
class Mcp2Context:
    """Everything a Phase 2 MCP tool needs: a session factory for the
    canonical Postgres store, the master key box for decrypting model
    credentials (``None`` when ``MONTAUK_MASTER_KEY`` is unset -- briefings
    then fall back to deterministic evidence), retrieval tuning, and the
    embedding provider for semantic search (``None`` = the process-wide
    local provider, loaded on first use).
    """

    session_factory: sessionmaker[Session]
    secret_box: SecretBox | None = None
    retrieval: RetrievalConfig | None = None
    max_candidates: int = 8
    embedding_provider: EmbeddingProvider | None = None
    # Cosine floor for search_people. Calibrated for all-MiniLM-L6-v2 in
    # Phase 1: clearly related short texts score ~0.45-0.75, unrelated
    # pairs ~-0.1-0.05.
    similarity_threshold: float = 0.35

    @property
    def retrieval_config(self) -> RetrievalConfig:
        return self.retrieval or RetrievalConfig()

    def semantic_index(self, scope: WorkspaceScope) -> tuple[SemanticIndex | None, str | None]:
        """The workspace's semantic index, or ``(None, reason)`` when
        semantic search is disabled or the provider can't be loaded --
        callers then serve lexical results and report the reason."""
        rcfg = self.retrieval_config
        if not rcfg.semantic_enabled:
            return None, "semantic search disabled; lexical results only"
        provider = self.embedding_provider
        if provider is None:
            from ..embeddings.local import default_provider

            try:
                provider = default_provider()
            except Exception as exc:  # noqa: BLE001 -- degrade to lexical, never fail the request
                return None, f"semantic provider unavailable ({type(exc).__name__}); lexical results only"
        return (
            SemanticIndex(
                scope,
                provider,
                interaction_chunk_tokens=rcfg.interaction_chunk_tokens,
                interaction_chunk_overlap_tokens=rcfg.interaction_chunk_overlap_tokens,
            ),
            None,
        )
