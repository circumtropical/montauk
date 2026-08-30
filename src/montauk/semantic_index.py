"""Derived, fully disposable semantic/vector index (spec sections 19, 20).

Chunks are small: the person summary, each fact's text, and each
interaction's summary -- never whole records, and never deterministic
structured fields like birthdays or phone numbers (spec section 19).

Vector storage is brute-force cosine similarity over an in-memory numpy
matrix persisted to vectors.npy, with parallel chunk metadata in a
separate chunks.sqlite (kept apart from the relational index so
`rebuild-index` and `rebuild-vectors` stay independently invalidatable).
This is appropriate at the expected personal-relationship-memory scale
(hundreds to low thousands of chunks); an ANN index would be premature
here, and faiss-cpu has no linux wheel for this Python version anyway.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .embeddings.base import EmbeddingProvider
from .models import Person
from .reconciliation import ScanResult

logger = logging.getLogger(__name__)

CHUNKS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chunks (
  row_index    INTEGER PRIMARY KEY,
  chunk_id     TEXT UNIQUE NOT NULL,
  person_id    TEXT NOT NULL,
  chunk_type   TEXT NOT NULL CHECK (chunk_type IN ('summary','fact','interaction')),
  local_id     TEXT,
  text         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_person ON chunks(person_id);

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


@dataclass(frozen=True, slots=True)
class SemanticMatch:
    chunk_id: str
    person_id: str
    chunk_type: str
    local_id: str | None
    text: str
    score: float


def chunk_person(person: Person) -> list[Chunk]:
    chunks: list[Chunk] = []
    if person.summary:
        chunks.append(
            Chunk(chunk_id=f"{person.id}:summary", person_id=person.id, chunk_type="summary", local_id=None, text=person.summary)
        )
    for fact in person.facts:
        chunks.append(
            Chunk(chunk_id=f"{person.id}:{fact.id}", person_id=person.id, chunk_type="fact", local_id=fact.id, text=fact.text)
        )
    for interaction in person.interactions:
        if interaction.summary:
            chunks.append(
                Chunk(
                    chunk_id=f"{person.id}:{interaction.id}",
                    person_id=person.id,
                    chunk_type="interaction",
                    local_id=interaction.id,
                    text=interaction.summary,
                )
            )
    return chunks


def _cosine_similarity(query_vector: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    if len(matrix) == 0:
        return np.zeros(0, dtype="float32")
    query_norm = query_vector / (np.linalg.norm(query_vector) + 1e-10)
    matrix_norms = matrix / (np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-10)
    return matrix_norms @ query_norm


class SemanticIndex:
    def __init__(self, index_dir: Path | str, embedding_provider: EmbeddingProvider):
        self.index_dir = Path(index_dir)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.vectors_path = self.index_dir / "vectors.npy"
        self.chunks_db_path = self.index_dir / "chunks.sqlite"
        self.embedding_provider = embedding_provider

        self._conn = sqlite3.connect(self.chunks_db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(CHUNKS_SCHEMA_SQL)
        self._conn.commit()
        self._vectors = self._load_vectors()

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

    def __enter__(self) -> SemanticIndex:
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

    def chunk_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]

    def rebuild_from_scan(self, scan_result: ScanResult) -> None:
        """Full rebuild: re-chunk and re-embed every valid person from
        scratch. Used by `montauk rebuild-vectors` and directly targets
        the "delete and rebuild produces an equivalent index" acceptance
        criterion, mirroring sqlite_index.rebuild_from_scan.
        """
        all_chunks: list[Chunk] = []
        for person_id in sorted(scan_result.valid):
            all_chunks.extend(chunk_person(scan_result.valid[person_id]))

        if all_chunks:
            matrix = np.array(self.embedding_provider.embed([c.text for c in all_chunks]), dtype="float32")
        else:
            matrix = np.zeros((0, self.embedding_provider.dimension), dtype="float32")

        self._conn.execute("DELETE FROM chunks")
        for row_index, chunk in enumerate(all_chunks):
            self._conn.execute(
                "INSERT INTO chunks (row_index, chunk_id, person_id, chunk_type, local_id, text) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (row_index, chunk.chunk_id, chunk.person_id, chunk.chunk_type, chunk.local_id, chunk.text),
            )
        self._set_meta("model_name", getattr(self.embedding_provider, "model_name", "unknown"))
        self._set_meta("dimension", str(self.embedding_provider.dimension))
        self._conn.commit()

        self._vectors = matrix
        self._save()

    def remove_person(self, person_id: str) -> None:
        """Remove every chunk belonging to `person_id` and keep row_index
        contiguous with the vector matrix (archive_person calls this
        directly; spec section 16 requires archived people vanish from
        the vector index too)."""
        rows = self._conn.execute("SELECT row_index FROM chunks WHERE person_id = ?", (person_id,)).fetchall()
        if not rows:
            return
        remove_indices = {r["row_index"] for r in rows}
        keep_mask = np.ones(len(self._vectors), dtype=bool)
        for idx in remove_indices:
            keep_mask[idx] = False
        self._vectors = self._vectors[keep_mask]

        self._conn.execute("DELETE FROM chunks WHERE person_id = ?", (person_id,))
        remaining = self._conn.execute("SELECT chunk_id FROM chunks ORDER BY row_index").fetchall()
        for new_index, row in enumerate(remaining):
            self._conn.execute("UPDATE chunks SET row_index = ? WHERE chunk_id = ?", (new_index, row["chunk_id"]))
        self._conn.commit()
        self._save()

    def upsert_person(self, person: Person) -> None:
        """Re-derive chunks for one person: drop any existing chunks for
        them, re-embed their current facts/interactions/summary, and
        append. Called after every canonical write in the write path."""
        self.remove_person(person.id)
        new_chunks = chunk_person(person)
        if not new_chunks:
            return
        matrix = np.array(self.embedding_provider.embed([c.text for c in new_chunks]), dtype="float32")
        start_row = len(self._vectors)
        for offset, chunk in enumerate(new_chunks):
            self._conn.execute(
                "INSERT INTO chunks (row_index, chunk_id, person_id, chunk_type, local_id, text) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (start_row + offset, chunk.chunk_id, chunk.person_id, chunk.chunk_type, chunk.local_id, chunk.text),
            )
        self._conn.commit()
        self._vectors = np.vstack([self._vectors, matrix]) if len(self._vectors) else matrix
        self._save()

    def search(self, query: str, *, limit: int = 5, similarity_threshold: float = 0.0) -> list[SemanticMatch]:
        if len(self._vectors) == 0 or not query.strip():
            return []
        query_vector = np.array(self.embedding_provider.embed([query])[0], dtype="float32")
        scores = _cosine_similarity(query_vector, self._vectors)
        rows = self._conn.execute("SELECT * FROM chunks ORDER BY row_index").fetchall()
        order = np.argsort(-scores)
        matches: list[SemanticMatch] = []
        for idx in order:
            score = float(scores[idx])
            if score < similarity_threshold:
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
                )
            )
            if len(matches) >= limit:
                break
        return matches
