"""Derived, fully disposable semantic/vector index (spec sections 19, 20).

Chunks are small semantic units: the person summary, each individual
fact, and each interaction summary -- never whole records, and never
deterministic structured fields like birthdays or phone numbers. A long
interaction summary is split at sentence boundaries into overlapping
chunks (each still pointing at the one canonical interaction_id).

Vectors live in PostgreSQL (``semantic_chunks``, a plain float32 ``bytea``
column -- no pgvector) next to the canonical records, so the dashboard and
the MCP server -- both of which write people -- share one index
(ADR 0005). Similarity is brute-force cosine over a numpy matrix loaded
per query, which is appropriate at personal-relationship-memory scale
(hundreds to low thousands of chunks).

The index reconciles on read: before a search, each person in scope is
re-chunked and hashed (chunk texts + embedding model + chunking config);
a hash that differs from ``semantic_person_state`` -- an edit from either
process, a model change, a chunking change -- re-embeds just that person.
Deleting both tables and searching again rebuilds an equivalent index.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from sqlalchemy import delete, func, insert, select, text

from .db import mapping
from .db import models as orm
from .db.repositories import PeopleRepository, WorkspaceScope
from .embeddings.base import EmbeddingProvider
from .models import Person
from .tokens import estimate_tokens, split_sentences

logger = logging.getLogger(__name__)

# Bump when the chunk-id scheme or chunk contents change so every person's
# content hash changes and is re-embedded on next read.
SCHEMA_VERSION = "3"

DEFAULT_INTERACTION_CHUNK_TOKENS = 120
DEFAULT_INTERACTION_CHUNK_OVERLAP_TOKENS = 24

# First key of the two-int advisory lock serializing index writes per
# workspace (second key: hashtext(workspace_id)). Arbitrary, but fixed.
_LOCK_NAMESPACE = 0x5E3A


@dataclass(frozen=True, slots=True)
class Chunk:
    chunk_id: str
    person_id: str
    chunk_type: str  # "summary" | "fact" | "interaction"
    local_id: str | None
    text: str
    chunk_index: int = 0
    chunk_total: int = 1


@dataclass(frozen=True, slots=True)
class SemanticMatch:
    chunk_id: str
    person_id: str
    chunk_type: str
    local_id: str | None
    text: str
    score: float
    chunk_index: int = 0
    chunk_total: int = 1


def _chunk_long_text(text: str, *, chunk_tokens: int, overlap_tokens: int) -> list[str]:
    """Sentence-window chunking: accumulate whole sentences up to
    `chunk_tokens`, then start the next window `overlap_tokens` worth of
    trailing sentences back so meaning isn't lost at the seam. Never
    duplicates a whole window."""
    sentences = split_sentences(text)
    if not sentences:
        return [" ".join(text.split())]
    windows: list[list[str]] = []
    current: list[str] = []
    current_tokens = 0
    for sentence in sentences:
        s_tokens = estimate_tokens(sentence)
        if current and current_tokens + s_tokens > chunk_tokens:
            windows.append(current)
            # carry the tail back for overlap
            carry: list[str] = []
            carry_tokens = 0
            for prev in reversed(current):
                pt = estimate_tokens(prev)
                if carry_tokens + pt > overlap_tokens:
                    break
                carry.insert(0, prev)
                carry_tokens += pt
            current = carry
            current_tokens = carry_tokens
        current.append(sentence)
        current_tokens += s_tokens
    if current:
        windows.append(current)
    joined = [" ".join(w) for w in windows]
    # Drop a trailing window that is wholly contained in its predecessor
    # (can happen when overlap >= remaining content).
    if len(joined) >= 2 and joined[-1] in joined[-2]:
        joined.pop()
    return joined


def chunk_person(
    person: Person,
    *,
    interaction_chunk_tokens: int = DEFAULT_INTERACTION_CHUNK_TOKENS,
    interaction_chunk_overlap_tokens: int = DEFAULT_INTERACTION_CHUNK_OVERLAP_TOKENS,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    if person.summary:
        chunks.append(
            Chunk(
                chunk_id=f"{person.id}:summary",
                person_id=person.id,
                chunk_type="summary",
                local_id=None,
                text=person.summary,
            )
        )
    for fact in person.facts:
        chunks.append(
            Chunk(
                chunk_id=f"{person.id}:{fact.id}",
                person_id=person.id,
                chunk_type="fact",
                local_id=fact.id,
                text=fact.text,
            )
        )
    for interaction in person.interactions:
        if not interaction.summary:
            continue
        pieces = (
            [interaction.summary]
            if estimate_tokens(interaction.summary) <= interaction_chunk_tokens
            else _chunk_long_text(
                interaction.summary,
                chunk_tokens=interaction_chunk_tokens,
                overlap_tokens=interaction_chunk_overlap_tokens,
            )
        )
        total = len(pieces)
        for idx, piece in enumerate(pieces):
            chunk_id = (
                f"{person.id}:{interaction.id}" if total == 1 else f"{person.id}:{interaction.id}#{idx}"
            )
            chunks.append(
                Chunk(
                    chunk_id=chunk_id,
                    person_id=person.id,
                    chunk_type="interaction",
                    local_id=interaction.id,
                    text=piece,
                    chunk_index=idx,
                    chunk_total=total,
                )
            )
    return chunks


def _cosine_similarity(query_vector: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    if len(matrix) == 0:
        return np.zeros(0, dtype="float32")
    query_norm = query_vector / (np.linalg.norm(query_vector) + 1e-10)
    matrix_norms = matrix / (np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-10)
    return matrix_norms @ query_norm


_MATCH_COLUMNS = (
    orm.SemanticChunk.chunk_id,
    orm.SemanticChunk.chunk_type,
    orm.SemanticChunk.local_id,
    orm.SemanticChunk.chunk_index,
    orm.SemanticChunk.chunk_total,
    orm.SemanticChunk.text,
    orm.SemanticChunk.embedding,
    orm.Person.public_id,
)


class SemanticIndex:
    """One workspace's view of the semantic index, bound to a request's
    session. Writes (re-embedding stale people) happen inside a savepoint
    under a per-workspace advisory lock and become durable when the
    caller's transaction commits."""

    def __init__(
        self,
        scope: WorkspaceScope,
        embedding_provider: EmbeddingProvider,
        *,
        interaction_chunk_tokens: int = DEFAULT_INTERACTION_CHUNK_TOKENS,
        interaction_chunk_overlap_tokens: int = DEFAULT_INTERACTION_CHUNK_OVERLAP_TOKENS,
    ):
        self.scope = scope
        self.session = scope.session
        self.workspace_id = scope.workspace_id
        self.embedding_provider = embedding_provider
        self.interaction_chunk_tokens = interaction_chunk_tokens
        self.interaction_chunk_overlap_tokens = interaction_chunk_overlap_tokens

    # -- fingerprints -----------------------------------------------------

    @property
    def model_name(self) -> str:
        return str(getattr(self.embedding_provider, "model_name", "unknown"))

    def chunk_fingerprint(self) -> str:
        return f"iact:{self.interaction_chunk_tokens}/{self.interaction_chunk_overlap_tokens}"

    def _chunk_person(self, person: Person) -> list[Chunk]:
        return chunk_person(
            person,
            interaction_chunk_tokens=self.interaction_chunk_tokens,
            interaction_chunk_overlap_tokens=self.interaction_chunk_overlap_tokens,
        )

    def content_hash(self, chunks: Sequence[Chunk]) -> str:
        h = hashlib.sha256()
        h.update(
            "\x1f".join(
                (
                    SCHEMA_VERSION,
                    self.model_name,
                    str(self.embedding_provider.dimension),
                    self.chunk_fingerprint(),
                )
            ).encode("utf-8")
        )
        for c in chunks:
            fields = (
                c.chunk_id,
                c.chunk_type,
                c.local_id or "",
                str(c.chunk_index),
                str(c.chunk_total),
                c.text,
            )
            h.update(("\x1e" + "\x1f".join(fields)).encode("utf-8"))
        return h.hexdigest()

    # -- reconcile ----------------------------------------------------------

    def _stored_hashes(self, person_uuids: Iterable[Any]) -> dict[Any, str]:
        ids = list(person_uuids)
        if not ids:
            return {}
        rows = self.session.execute(
            select(orm.SemanticPersonState.person_id, orm.SemanticPersonState.content_hash).where(
                orm.SemanticPersonState.person_id.in_(ids)
            )
        ).all()
        return {pid: h for pid, h in rows}

    def _pending(self, rows: Sequence[orm.Person]) -> list[tuple[orm.Person, list[Chunk], str]]:
        stored = self._stored_hashes(r.id for r in rows)
        pending = []
        for row in rows:
            chunks = self._chunk_person(mapping.person_to_domain(row))
            digest = self.content_hash(chunks)
            if stored.get(row.id) != digest:
                pending.append((row, chunks, digest))
        return pending

    def sync(self, rows: Iterable[orm.Person]) -> int:
        """Re-embed every given person whose indexed content is stale.
        Returns how many people were (re-)embedded. Embedding happens
        before any write, so a provider failure leaves the index as it
        was; a write failure rolls back only this savepoint."""
        pending = self._pending(list(rows))
        if not pending:
            return 0
        texts = [c.text for _row, chunks, _h in pending for c in chunks]
        vectors: np.ndarray | None = (
            np.asarray(self.embedding_provider.embed(texts), dtype="<f4") if texts else None
        )
        dimension = self.embedding_provider.dimension
        person_uuids = [row.id for row, _c, _h in pending]
        chunk_rows: list[dict[str, Any]] = []
        state_rows: list[dict[str, Any]] = []
        i = 0
        for row, chunks, digest in pending:
            for c in chunks:
                assert vectors is not None
                chunk_rows.append(
                    {
                        "workspace_id": self.workspace_id,
                        "person_id": row.id,
                        "chunk_id": c.chunk_id,
                        "chunk_type": c.chunk_type,
                        "local_id": c.local_id,
                        "chunk_index": c.chunk_index,
                        "chunk_total": c.chunk_total,
                        "text": c.text,
                        "embedding": vectors[i].tobytes(),
                    }
                )
                i += 1
            state_rows.append(
                {
                    "person_id": row.id,
                    "workspace_id": self.workspace_id,
                    "content_hash": digest,
                    "model_name": self.model_name,
                    "dimension": dimension,
                    "chunk_fingerprint": self.chunk_fingerprint(),
                    "chunk_count": len(chunks),
                }
            )
        with self.session.begin_nested():
            self._lock()
            self.session.execute(
                delete(orm.SemanticChunk).where(orm.SemanticChunk.person_id.in_(person_uuids))
            )
            self.session.execute(
                delete(orm.SemanticPersonState).where(orm.SemanticPersonState.person_id.in_(person_uuids))
            )
            if chunk_rows:
                self.session.execute(insert(orm.SemanticChunk), chunk_rows)
            self.session.execute(insert(orm.SemanticPersonState), state_rows)
        logger.info("semantic index: re-embedded %d people (%d chunks)", len(pending), len(chunk_rows))
        return len(pending)

    def _lock(self) -> None:
        """Serialize index writes per workspace across processes: without
        it, two concurrent re-embeds of one person both insert and the
        second commit trips the (person_id, chunk_id) unique constraint."""
        self.session.execute(
            text("SELECT pg_advisory_xact_lock(:ns, hashtext(:ws))"),
            {"ns": _LOCK_NAMESPACE, "ws": str(self.workspace_id)},
        )

    def sync_workspace(self) -> int:
        return self.sync(PeopleRepository(self.scope).list_people(archived=False))

    def rebuild(self) -> int:
        """Drop this workspace's index and re-embed every active person."""
        with self.session.begin_nested():
            self._lock()
            self.session.execute(
                delete(orm.SemanticChunk).where(orm.SemanticChunk.workspace_id == self.workspace_id)
            )
            self.session.execute(
                delete(orm.SemanticPersonState).where(
                    orm.SemanticPersonState.workspace_id == self.workspace_id
                )
            )
        return self.sync_workspace()

    # -- introspection ------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Counts and fingerprints only -- never personal content."""
        active = PeopleRepository(self.scope).list_people(archived=False)
        chunk_count = self.session.execute(
            select(func.count())
            .select_from(orm.SemanticChunk)
            .where(orm.SemanticChunk.workspace_id == self.workspace_id)
        ).scalar_one()
        indexed = self.session.execute(
            select(func.count())
            .select_from(orm.SemanticPersonState)
            .join(orm.Person, orm.Person.id == orm.SemanticPersonState.person_id)
            .where(orm.SemanticPersonState.workspace_id == self.workspace_id)
            .where(orm.Person.archived_at.is_(None))
        ).scalar_one()
        return {
            "model_name": self.model_name,
            "dimension": self.embedding_provider.dimension,
            "chunk_fingerprint": self.chunk_fingerprint(),
            "active_people": len(active),
            "indexed_people": indexed,
            "chunks": chunk_count,
            "stale_person_ids": sorted(row.public_id for row, _c, _h in self._pending(active)),
        }

    # -- search -------------------------------------------------------------

    def _rank(self, query: str, rows: Sequence[Any], *, limit: int, threshold: float) -> list[SemanticMatch]:
        dimension = self.embedding_provider.dimension
        usable = [r for r in rows if len(r.embedding) == dimension * 4]
        if not usable:
            return []
        matrix = np.vstack([np.frombuffer(r.embedding, dtype="<f4") for r in usable])
        query_vector: np.ndarray = np.asarray(self.embedding_provider.embed([query])[0], dtype="float32")
        scores = _cosine_similarity(query_vector, matrix)
        matches: list[SemanticMatch] = []
        for idx in np.argsort(-scores):
            score = float(scores[idx])
            if score < threshold:
                break
            r = usable[idx]
            matches.append(
                SemanticMatch(
                    chunk_id=r.chunk_id,
                    person_id=r.public_id,
                    chunk_type=r.chunk_type,
                    local_id=r.local_id,
                    text=r.text,
                    score=score,
                    chunk_index=r.chunk_index,
                    chunk_total=r.chunk_total,
                )
            )
            if len(matches) >= limit:
                break
        return matches

    def search(self, query: str, *, limit: int = 5, similarity_threshold: float = 0.0) -> list[SemanticMatch]:
        """Cosine similarity across every active person in the workspace."""
        if not query.strip():
            return []
        self.sync_workspace()
        rows = self.session.execute(
            select(*_MATCH_COLUMNS)
            .join(orm.Person, orm.Person.id == orm.SemanticChunk.person_id)
            .where(orm.SemanticChunk.workspace_id == self.workspace_id)
            .where(orm.Person.archived_at.is_(None))
        ).all()
        return self._rank(query, rows, limit=limit, threshold=similarity_threshold)

    def search_person(
        self, query: str, person_id: str, *, limit: int = 25, similarity_threshold: float = 0.0
    ) -> list[SemanticMatch]:
        """Cosine similarity restricted to one person's chunks (spec: after
        person_id is resolved, retrieval never crosses people)."""
        if not query.strip():
            return []
        row = PeopleRepository(self.scope).get(person_id, include_archived=True)
        if row is None:
            return []
        self.sync([row])
        rows = self.session.execute(
            select(*_MATCH_COLUMNS)
            .join(orm.Person, orm.Person.id == orm.SemanticChunk.person_id)
            .where(orm.SemanticChunk.person_id == row.id)
        ).all()
        return self._rank(query, rows, limit=limit, threshold=similarity_threshold)
