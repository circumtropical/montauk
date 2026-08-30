"""Embedding provider interface (spec section 20).

Implementation-neutral: semantic_index.py depends only on this Protocol,
never on fastembed directly, so a hosted/API provider can be added later
as an optional adapter without touching chunking/indexing code.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingProvider(Protocol):
    @property
    def dimension(self) -> int: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...
