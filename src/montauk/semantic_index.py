"""Derived, fully disposable semantic/vector index (spec sections 19, 20).

Chunks are small semantic units: the person summary, each individual
fact, and each interaction summary -- never whole records, and never
deterministic structured fields like birthdays or phone numbers. A long
interaction summary is split at sentence boundaries into overlapping
chunks (each still pointing at the one canonical interaction_id).

Vector storage is brute-force cosine similarity over an in-memory numpy
matrix persisted to vectors.npy, with parallel chunk metadata in a
separate chunks.sqlite. This is appropriate at the expected
personal-relationship-memory scale (hundreds to low thousands of chunks).

The index is derived: deleting it and rebuilding from canonical Markdown
must produce an equivalent index. `person_meta.content_hash` lets a
reader tell whether a person's chunks are current with their canonical
file; `vector_meta` records the schema version, embedding model, and
chunking configuration so drift can be detected at startup.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Self

import numpy as np

from .embeddings.base import EmbeddingProvider
from .models import Person
from .tokens import estimate_tokens, split_sentences

logger = logging.getLogger(__name__)

# Bump when the chunks table shape or chunk-id scheme changes so startup
# validation forces a rebuild.
SCHEMA_VERSION = "2"

DEFAULT_INTERACTION_CHUNK_TOKENS = 120
DEFAULT_INTERACTION_CHUNK_OVERLAP_TOKENS = 24

CHUNKS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chunks (
  row_index    INTEGER PRIMARY KEY,
  chunk_id     TEXT UNIQUE NOT NULL,
  person_id    TEXT NOT NULL,
  chunk_type   TEXT NOT NULL CHECK (chunk_type IN ('summary','fact','interaction')),
  local_id     TEXT,
  chunk_index  INTEGER NOT NULL DEFAULT 0,
  chunk_total  INTEGER NOT NULL DEFAULT 1,
  text         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_person ON chunks(person_id);

CREATE TABLE IF NOT EXISTS person_meta (
  person_id    TEXT PRIMARY KEY,
  content_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vector_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


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


_CHUNK_COLUMNS = (
    "row_index",
    "chunk_id",
    "person_id",
    "chunk_type",
    "local_id",
    "chunk_index",
    "chunk_total",
    "text",
)


class SemanticIndex:
    def __init__(
        self,
        index_dir: Path | str,
        embedding_provider: EmbeddingProvider,
        *,
        interaction_chunk_tokens: int = DEFAULT_INTERACTION_CHUNK_TOKENS,
        interaction_chunk_overlap_tokens: int = DEFAULT_INTERACTION_CHUNK_OVERLAP_TOKENS,
    ):
        self.index_dir = Path(index_dir)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.vectors_path = self.index_dir / "vectors.npy"
        self.chunks_db_path = self.index_dir / "chunks.sqlite"
        self.embedding_provider = embedding_provider
        self.interaction_chunk_tokens = interaction_chunk_tokens
        self.interaction_chunk_overlap_tokens = interaction_chunk_overlap_tokens

        self._conn = sqlite3.connect(self.chunks_db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._migrate_physical_schema()
        self._conn.executescript(CHUNKS_SCHEMA_SQL)
        self._conn.commit()
        self._vectors = self._load_vectors()

    def _migrate_physical_schema(self) -> None:
        """A pre-v2 chunks table lacks chunk_index/chunk_total and the
        person_meta table. The index is derived, so the safe migration is
        to drop the stale physical tables and let startup reconciliation
        repopulate them from canonical Markdown."""
        existing = {r[0] for r in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "chunks" not in existing:
            return
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(chunks)")}
        if {"chunk_index", "chunk_total"} <= cols and "person_meta" in existing:
            return
        logger.warning(
            "semantic index chunks.sqlite is a pre-v%s schema; dropping for rebuild", SCHEMA_VERSION
        )
        self._conn.execute("DROP TABLE IF EXISTS chunks")
        self._conn.execute("DROP TABLE IF EXISTS person_meta")
        self._conn.execute("DELETE FROM vector_meta")
        self._conn.commit()
        if self.vectors_path.exists():
            self.vectors_path.unlink()

    # -- chunk config fingerprint -------------------------------------------

    def chunk_fingerprint(self) -> str:
        return f"iact:{self.interaction_chunk_tokens}/{self.interaction_chunk_overlap_tokens}"

    def _chunk_person(self, person: Person) -> list[Chunk]:
        return chunk_person(
            person,
            interaction_chunk_tokens=self.interaction_chunk_tokens,
            interaction_chunk_overlap_tokens=self.interaction_chunk_overlap_tokens,
        )

    def _load_vectors(self) -> np.ndarray:
        if self.vectors_path.exists():
            vectors = np.load(self.vectors_path)
            if vectors.ndim == 2 and vectors.shape[1] == self.embedding_provider.dimension:
                return vectors
            logger.warning(
                "vectors.npy dimension %s doesn't match provider dimension %s; starting empty until rebuild",
                vectors.shape[1:] if vectors.ndim == 2 else vectors.shape,
                self.embedding_provider.dimension,
            )
        return np.zeros((0, self.embedding_provider.dimension), dtype="float32")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _save(self) -> None:
        np.save(self.vectors_path, self._vectors)

    def _set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO vector_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM vector_meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def _write_fingerprint_meta(self) -> None:
        self._set_meta("schema_version", SCHEMA_VERSION)
        self._set_meta("model_name", getattr(self.embedding_provider, "model_name", "unknown"))
        self._set_meta("dimension", str(self.embedding_provider.dimension))
        self._set_meta("chunk_fingerprint", self.chunk_fingerprint())

    def chunk_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]

    def person_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) AS n FROM person_meta").fetchone()["n"]

    def person_content_hash(self, person_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT content_hash FROM person_meta WHERE person_id = ?", (person_id,)
        ).fetchone()
        return row["content_hash"] if row else None

    # -- drift detection --------------------------------------------------

    def stale_reason(self) -> str | None:
        """Why the whole index is unusable as *semantic* results, or None
        if its fingerprint matches the current provider/schema/chunking."""
        if self.get_meta("schema_version") not in (None, SCHEMA_VERSION):
            return f"schema version changed ({self.get_meta('schema_version')} -> {SCHEMA_VERSION})"
        stored_model = self.get_meta("model_name")
        current_model = getattr(self.embedding_provider, "model_name", "unknown")
        if stored_model is not None and stored_model != current_model:
            return f"embedding model changed ({stored_model} -> {current_model})"
        stored_dim = self.get_meta("dimension")
        if stored_dim is not None and stored_dim != str(self.embedding_provider.dimension):
            return f"embedding dimension changed ({stored_dim} -> {self.embedding_provider.dimension})"
        stored_fp = self.get_meta("chunk_fingerprint")
        if stored_fp is not None and stored_fp != self.chunk_fingerprint():
            return f"chunking configuration changed ({stored_fp} -> {self.chunk_fingerprint()})"
        if self._vectors.shape[1:] and self._vectors.shape[1] != self.embedding_provider.dimension:
            return "vector matrix dimension does not match the provider"
        if len(self._vectors) != self.chunk_count():
            return "vector matrix and chunk table are out of sync"
        return None

    def orphan_person_ids(self, active_person_ids: set[str]) -> set[str]:
        indexed = {r["person_id"] for r in self._conn.execute("SELECT DISTINCT person_id FROM chunks")}
        indexed |= {r["person_id"] for r in self._conn.execute("SELECT person_id FROM person_meta")}
        return indexed - active_person_ids

    def index_state(self, *, active_person_ids: set[str] | None = None) -> dict:
        state = {
            "schema_version": self.get_meta("schema_version"),
            "model_name": self.get_meta("model_name"),
            "dimension": self.get_meta("dimension"),
            "chunk_fingerprint": self.get_meta("chunk_fingerprint"),
            "chunk_count": self.chunk_count(),
            "person_count": self.person_count(),
            "vector_rows": len(self._vectors),
            "stale_reason": self.stale_reason(),
        }
        if active_person_ids is not None:
            state["orphan_person_ids"] = sorted(self.orphan_person_ids(active_person_ids))
        return state

    # -- bulk / incremental writes --------------------------------------

    def _insert_chunks(self, chunks: list[Chunk], start_row: int) -> None:
        cols = ", ".join(_CHUNK_COLUMNS)
        placeholders = ", ".join("?" for _ in _CHUNK_COLUMNS)
        self._conn.executemany(
            f"INSERT INTO chunks ({cols}) VALUES ({placeholders})",
            [
                (
                    start_row + i,
                    c.chunk_id,
                    c.person_id,
                    c.chunk_type,
                    c.local_id,
                    c.chunk_index,
                    c.chunk_total,
                    c.text,
                )
                for i, c in enumerate(chunks)
            ],
        )

    def rebuild(self, people: Mapping[str, Person], *, content_hashes: dict[str, str] | None = None) -> None:
        """Full rebuild from an already-validated ``{person_id: Person}`` map.
        `content_hashes` (person_id -> canonical record hash) lets readers
        detect drift; when omitted, person_meta rows are still written but
        with a sentinel."""
        content_hashes = content_hashes or {}
        all_chunks: list[Chunk] = []
        for person_id in sorted(people):
            all_chunks.extend(self._chunk_person(people[person_id]))

        if all_chunks:
            matrix = np.array(self.embedding_provider.embed([c.text for c in all_chunks]), dtype="float32")
        else:
            matrix = np.zeros((0, self.embedding_provider.dimension), dtype="float32")

        self._conn.execute("DELETE FROM chunks")
        self._conn.execute("DELETE FROM person_meta")
        self._insert_chunks(all_chunks, 0)
        for person_id in people:
            self._conn.execute(
                "INSERT INTO person_meta (person_id, content_hash) VALUES (?, ?)",
                (person_id, content_hashes.get(person_id, "unknown")),
            )
        self._write_fingerprint_meta()
        self._conn.commit()

        self._vectors = matrix
        self._save()

    def remove_person(self, person_id: str) -> None:
        rows = self._conn.execute("SELECT row_index FROM chunks WHERE person_id = ?", (person_id,)).fetchall()
        self._conn.execute("DELETE FROM person_meta WHERE person_id = ?", (person_id,))
        if not rows:
            self._conn.commit()
            return
        remove_indices = {r["row_index"] for r in rows}
        keep_mask = np.ones(len(self._vectors), dtype=bool)
        for idx in remove_indices:
            keep_mask[idx] = False
        self._vectors = self._vectors[keep_mask]

        self._conn.execute("DELETE FROM chunks WHERE person_id = ?", (person_id,))
        remaining = self._conn.execute("SELECT chunk_id FROM chunks ORDER BY row_index").fetchall()
        for new_index, row in enumerate(remaining):
            self._conn.execute(
                "UPDATE chunks SET row_index = ? WHERE chunk_id = ?", (new_index, row["chunk_id"])
            )
        self._conn.commit()
        self._save()

    def upsert_person(self, person: Person, *, content_hash: str | None = None) -> None:
        """Re-derive one person's chunks: drop the old ones, re-embed, append."""
        self.remove_person(person.id)
        new_chunks = self._chunk_person(person)
        self._conn.execute(
            "INSERT INTO person_meta (person_id, content_hash) VALUES (?, ?) "
            "ON CONFLICT(person_id) DO UPDATE SET content_hash = excluded.content_hash",
            (person.id, content_hash or "unknown"),
        )
        if not self.get_meta("schema_version"):
            self._write_fingerprint_meta()
        if not new_chunks:
            self._conn.commit()
            return
        matrix = np.array(self.embedding_provider.embed([c.text for c in new_chunks]), dtype="float32")
        start_row = len(self._vectors)
        self._insert_chunks(new_chunks, start_row)
        self._conn.commit()
        self._vectors = np.vstack([self._vectors, matrix]) if len(self._vectors) else matrix
        self._save()

    # -- search --------------------------------------------------------

    def _row_matches(
        self, rows: list, scores: np.ndarray, *, limit: int, threshold: float
    ) -> list[SemanticMatch]:
        order = np.argsort(-scores)
        matches: list[SemanticMatch] = []
        for idx in order:
            score = float(scores[idx])
            if score < threshold:
                break
            row = rows[idx]
            matches.append(
                SemanticMatch(
                    chunk_id=row["chunk_id"],
                    person_id=row["person_id"],
                    chunk_type=row["chunk_type"],
                    local_id=row["local_id"],
                    text=row["text"],
                    score=score,
                    chunk_index=row["chunk_index"],
                    chunk_total=row["chunk_total"],
                )
            )
            if len(matches) >= limit:
                break
        return matches

    def search(self, query: str, *, limit: int = 5, similarity_threshold: float = 0.0) -> list[SemanticMatch]:
        if len(self._vectors) == 0 or not query.strip():
            return []
        query_vector = np.array(self.embedding_provider.embed([query])[0], dtype="float32")
        scores = _cosine_similarity(query_vector, self._vectors)
        rows = self._conn.execute("SELECT * FROM chunks ORDER BY row_index").fetchall()
        return self._row_matches(rows, scores, limit=limit, threshold=similarity_threshold)

    def search_person(
        self, query: str, person_id: str, *, limit: int = 25, similarity_threshold: float = 0.0
    ) -> list[SemanticMatch]:
        """Cosine similarity restricted to one person's chunks (spec: after
        person_id is resolved, retrieval never crosses people)."""
        if len(self._vectors) == 0 or not query.strip():
            return []
        rows = self._conn.execute(
            "SELECT * FROM chunks WHERE person_id = ? ORDER BY row_index", (person_id,)
        ).fetchall()
        if not rows:
            return []
        row_indices = [r["row_index"] for r in rows]
        query_vector = np.array(self.embedding_provider.embed([query])[0], dtype="float32")
        submatrix = self._vectors[row_indices]
        scores = _cosine_similarity(query_vector, submatrix)
        return self._row_matches(rows, scores, limit=limit, threshold=similarity_threshold)
