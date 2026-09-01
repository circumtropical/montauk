from montauk.bootstrap import build_context, reconcile_on_startup
from montauk.config import MontaukConfig
from montauk.markdown_store import MarkdownStore
from montauk.models import Person


def _config(tmp_path, **overrides) -> MontaukConfig:
    data = {"data_dir": str(tmp_path / "data")}
    data.update(overrides)
    return MontaukConfig.model_validate(data)


class TestBuildContext:
    def test_builds_working_stores_without_semantic_or_auth(self, tmp_path):
        config = _config(tmp_path)
        ctx = build_context(config, with_semantic=False, with_auth=False)
        assert ctx.semantic_index is None
        assert ctx.credential_store is None
        assert ctx.store.data_dir == config.data_dir_path

    def test_with_auth_creates_credential_store_under_data_dir(self, tmp_path):
        config = _config(tmp_path)
        ctx = build_context(config, with_semantic=False, with_auth=True)
        assert ctx.credential_store is not None
        assert (config.data_dir_path / "auth" / "credentials.sqlite").exists()

    def test_search_config_propagates_to_context(self, tmp_path):
        config = _config(tmp_path, search={"max_candidates": 3, "similarity_threshold": 0.7})
        ctx = build_context(config, with_semantic=False, with_auth=False)
        assert ctx.max_candidates == 3
        assert ctx.similarity_threshold == 0.7


class TestReconcileOnStartup:
    def test_populates_sqlite_index_from_markdown(self, tmp_path):
        config = _config(tmp_path)
        store = MarkdownStore(config.data_dir_path)
        store.write_person(Person(id="P0001", name="Homer Simpson"))
        ctx = build_context(config, with_semantic=False, with_auth=False)

        result = reconcile_on_startup(ctx)

        assert result.healthy is True
        assert ctx.sqlite_index.get_row("P0001") is not None

    def test_malformed_file_does_not_crash_startup(self, tmp_path):
        config = _config(tmp_path)
        store = MarkdownStore(config.data_dir_path)
        store.write_person(Person(id="P0001", name="Homer Simpson"))
        (store.people_dir / "broken.md").write_text("---\nid: [bad\n---\n\n# X\n")
        ctx = build_context(config, with_semantic=False, with_auth=False)

        result = reconcile_on_startup(ctx)

        assert result.healthy is False
        assert "P0001" in result.valid

    def test_semantic_index_incrementally_updated_not_fully_rebuilt(self, tmp_path):
        config = _config(tmp_path, search={"semantic_enabled": True})
        store = MarkdownStore(config.data_dir_path)
        store.write_person(Person(id="P0001", name="Homer Simpson", summary="Works at the plant."))
        ctx = build_context(config, with_semantic=True, with_auth=False)

        reconcile_on_startup(ctx)
        assert ctx.semantic_index.chunk_count() == 1

        # A second reconciliation with nothing changed must not touch the
        # existing chunk (no re-embed of unchanged people).
        row_before = ctx.semantic_index._conn.execute("SELECT row_index FROM chunks").fetchone()["row_index"]
        reconcile_on_startup(ctx)
        row_after = ctx.semantic_index._conn.execute("SELECT row_index FROM chunks").fetchone()["row_index"]
        assert row_before == row_after
        assert ctx.semantic_index.chunk_count() == 1

        # Adding a second person only adds their chunk, incrementally.
        store.write_person(Person(id="P0002", name="Marge Simpson", summary="Keeps the house running."))
        reconcile_on_startup(ctx)
        assert ctx.semantic_index.chunk_count() == 2

    def test_manual_hand_edit_becomes_active_after_reconciliation(self, tmp_path):
        # Spec acceptance criterion: manual Markdown edits made outside
        # any MCP tool call take effect after restart/reconciliation, not
        # immediately (Phase 1 has no filesystem watcher).
        config = _config(tmp_path)
        store = MarkdownStore(config.data_dir_path)
        store.write_person(Person(id="P0001", name="Homer Simpson", summary="Original summary."))
        ctx = build_context(config, with_semantic=False, with_auth=False)
        reconcile_on_startup(ctx)
        assert ctx.sqlite_index.get_row("P0001")["summary"] == "Original summary."

        # Simulate a human directly editing the file in a text editor,
        # bypassing MarkdownStore/write_queue entirely.
        path = store.person_path("P0001")
        path.write_text(path.read_text().replace("Original summary.", "Hand-edited summary."))

        result = reconcile_on_startup(ctx)

        assert result.valid["P0001"].summary == "Hand-edited summary."
        assert ctx.sqlite_index.get_row("P0001")["summary"] == "Hand-edited summary."
