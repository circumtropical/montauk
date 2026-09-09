"""Retrieval tuning for purpose-specific context selection.

The Phase 2 services (briefings, the MCP ``prepare_person_context`` tool)
take a ``RetrievalConfig`` to size the evidence they hand a model.
Everything operational -- the database DSN, the master key, the bind
address -- comes from the environment, not a config file.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class RetrievalConfig(BaseModel):
    """Purpose-specific context retrieval (prepare_person_context / briefings)."""

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
            raise ValueError(
                "retrieval.interaction_chunk_overlap_tokens must be below interaction_chunk_tokens"
            )
        return self

    def budget_for(self, detail_level: str) -> int:
        return {
            "brief": self.brief_tokens,
            "standard": self.standard_tokens,
            "comprehensive": self.comprehensive_tokens,
        }[detail_level]

    def chunk_fingerprint(self) -> str:
        return f"iact:{self.interaction_chunk_tokens}/{self.interaction_chunk_overlap_tokens}"
