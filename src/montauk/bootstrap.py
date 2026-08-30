"""Assembles a MontaukContext from a MontaukConfig and runs startup
reconciliation (spec section 18). Shared by `montauk serve` and the
admin CLI commands that need a working context.
"""

from __future__ import annotations

import logging

from .auth import CredentialStore
from .config import MontaukConfig
from .embeddings.local import LocalEmbeddingProvider
from .markdown_store import MarkdownStore
from .reconciliation import ScanResult, scan_people_directory, write_validation_report
from .semantic_index import SemanticIndex
from .sqlite_index import SqliteIndex
from .tools_core import MontaukContext
from .write_queue import WriteQueue

logger = logging.getLogger("montauk.bootstrap")


def build_context(config: MontaukConfig, *, with_semantic: bool | None = None, with_auth: bool = True) -> MontaukContext:
    """Construct stores/indexes for config.data_dir_path. Does not run
    reconciliation -- callers that want a fresh scan call
    reconcile_on_startup separately (some CLI commands want a context
    without paying for the semantic model, hence with_semantic)."""
    data_dir = config.data_dir_path
    store = MarkdownStore(data_dir)
    sqlite_index = SqliteIndex(data_dir / "index" / "relationships.sqlite")
    write_queue = WriteQueue(data_dir)

    semantic_index = None
    use_semantic = config.search.semantic_enabled if with_semantic is None else with_semantic
    if use_semantic:
        provider = LocalEmbeddingProvider(model_name=config.embedding.model)
        semantic_index = SemanticIndex(data_dir / "index" / "vectors", provider)

    credential_store = CredentialStore(data_dir / "auth" / "credentials.sqlite") if with_auth else None

    return MontaukContext(
        store=store,
        sqlite_index=sqlite_index,
        write_queue=write_queue,
        semantic_index=semantic_index,
        credential_store=credential_store,
        max_candidates=config.search.max_candidates,
        similarity_threshold=config.search.similarity_threshold,
    )


def reconcile_on_startup(ctx: MontaukContext) -> ScanResult:
    """Scan, validate, reconcile the SQLite index, and (incrementally --
    not a full rebuild) the semantic index if enabled. Never raises for
    malformed person files; only genuine IO failures propagate."""
    result = scan_people_directory(ctx.store)
    write_validation_report(result, ctx.store.data_dir)
    stats = ctx.sqlite_index.reconcile(ctx.store, result)
    logger.info(
        "startup reconciliation: %d valid, %d error(s), %d warning(s) "
        "(index: %d inserted, %d updated, %d unchanged, %d removed)",
        len(result.valid),
        len(result.errors),
        len(result.warnings),
        stats.inserted,
        stats.updated,
        stats.unchanged,
        stats.removed,
    )
    if ctx.semantic_index is not None:
        for person_id in stats.changed_person_ids:
            ctx.semantic_index.upsert_person(result.valid[person_id])
        for person_id in stats.removed_person_ids:
            ctx.semantic_index.remove_person(person_id)
    if not result.healthy:
        logger.warning("repository is degraded: %d error(s) found; see get_validation_errors", len(result.errors))
    return result
