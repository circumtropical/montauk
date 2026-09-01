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
from .sqlite_index import SqliteIndex, compute_content_hash
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
        semantic_index = SemanticIndex(
            data_dir / "index" / "vectors",
            provider,
            interaction_chunk_tokens=config.retrieval.interaction_chunk_tokens,
            interaction_chunk_overlap_tokens=config.retrieval.interaction_chunk_overlap_tokens,
        )

    credential_store = CredentialStore(data_dir / "auth" / "credentials.sqlite") if with_auth else None

    return MontaukContext(
        store=store,
        sqlite_index=sqlite_index,
        write_queue=write_queue,
        semantic_index=semantic_index,
        credential_store=credential_store,
        max_candidates=config.search.max_candidates,
        similarity_threshold=config.search.similarity_threshold,
        retrieval=config.retrieval,
    )


def _content_hashes(ctx: MontaukContext, result: ScanResult) -> dict[str, str]:
    out: dict[str, str] = {}
    for person_id in result.valid:
        try:
            out[person_id] = compute_content_hash(ctx.store.person_path(person_id))
        except OSError:
            continue
    return out


def reconcile_on_startup(ctx: MontaukContext) -> ScanResult:
    """Scan, validate, reconcile the SQLite index, and (incrementally --
    not a full rebuild) the semantic index if enabled. Never raises for
    malformed person files; only genuine IO failures propagate."""
    result = scan_people_directory(ctx.store)
    write_validation_report(result, ctx.store.data_dir)
    # Never hand out a generic person ID at or below one already on disk
    # (e.g. a file added by hand between restarts).
    if ctx.store.sync_id_sequence():
        logger.info("advanced person-id sequence to cover an id already present on disk")
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
        _reconcile_semantic_index(ctx, result, stats)
    if not result.healthy:
        logger.warning("repository is degraded: %d error(s) found; see get_validation_errors", len(result.errors))
    return result


def _reconcile_semantic_index(ctx: MontaukContext, result: ScanResult, stats) -> None:
    index = ctx.semantic_index
    assert index is not None
    hashes = _content_hashes(ctx, result)

    # 1. Fingerprint drift (model / schema / chunking): the whole index is
    #    no longer valid *semantic* evidence. The local provider can
    #    rebuild in-process; a future hosted provider would be an
    #    externally billed op, so there we degrade to lexical and wait for
    #    an explicit `montauk rebuild-index`.
    reason = index.stale_reason()
    if reason is not None:
        provider_name = getattr(index.embedding_provider, "provider", "local")
        if provider_name == "local":
            logger.warning("semantic index stale (%s); rebuilding from canonical Markdown", reason)
            index.rebuild_from_scan(result, content_hashes=hashes)
            ctx.semantic_stale_reason = None
            return
        logger.warning(
            "semantic index stale (%s) and provider %r cannot auto-rebuild; "
            "serving lexical-only until `montauk rebuild-index`",
            reason,
            provider_name,
        )
        ctx.semantic_stale_reason = f"{reason}; run `montauk rebuild-index`"
        return

    # 2. Incremental: re-embed changed people, drop removed ones, and
    #    catch any person whose stored hash no longer matches the file
    #    (e.g. a hand-edit stats.reconcile didn't flag as content-changed).
    changed = set(stats.changed_person_ids)
    for person_id in result.valid:
        if person_id in changed:
            continue
        if index.person_content_hash(person_id) != hashes.get(person_id):
            changed.add(person_id)
    for person_id in changed:
        index.upsert_person(result.valid[person_id], content_hash=hashes.get(person_id))
    for person_id in stats.removed_person_ids:
        index.remove_person(person_id)
    # orphans: chunks for people no longer active/valid
    for person_id in index.orphan_person_ids(set(result.valid)):
        logger.info("removing orphaned semantic entries for %s", person_id)
        index.remove_person(person_id)
    if not index.get_meta("schema_version"):
        index._write_fingerprint_meta()
        index._conn.commit()
