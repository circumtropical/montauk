import logging

from typer.testing import CliRunner

from montauk.cli import _maybe_warn_plaintext_remote, app
from montauk.markdown_store import MarkdownStore
from montauk.models import Person

runner = CliRunner()


def _data_dir_args(tmp_path) -> list[str]:
    return ["--data-dir", str(tmp_path / "data")]


class TestValidate:
    def test_empty_repository_is_healthy(self, tmp_path):
        result = runner.invoke(app, ["validate", *_data_dir_args(tmp_path)])
        assert result.exit_code == 0
        assert "healthy: True" in result.stdout

    def test_malformed_file_exits_nonzero_and_lists_issue(self, tmp_path):
        store = MarkdownStore(tmp_path / "data")
        (store.people_dir / "broken.md").write_text("---\nid: [bad\n---\n\n# X\n")
        result = runner.invoke(app, ["validate", *_data_dir_args(tmp_path)])
        assert result.exit_code == 1
        assert "healthy: False" in result.stdout
        assert "format_error" in result.stdout

    def test_writes_validation_report(self, tmp_path):
        runner.invoke(app, ["validate", *_data_dir_args(tmp_path)])
        assert (tmp_path / "data" / "validation-report.json").exists()


class TestStatus:
    def test_reports_counts(self, tmp_path):
        store = MarkdownStore(tmp_path / "data")
        store.write_person(Person(id="P0001", name="Homer Simpson"))
        result = runner.invoke(app, ["status", *_data_dir_args(tmp_path)])
        assert result.exit_code == 0
        assert "valid people: 1" in result.stdout


class TestRebuildIndex:
    def test_rebuilds_and_reports_count(self, tmp_path):
        store = MarkdownStore(tmp_path / "data")
        store.write_person(Person(id="P0001", name="Homer Simpson"))
        result = runner.invoke(app, ["rebuild-index", *_data_dir_args(tmp_path)])
        assert result.exit_code == 0
        assert "1 people indexed" in result.stdout

        from montauk.sqlite_index import SqliteIndex

        index = SqliteIndex(tmp_path / "data" / "index" / "relationships.sqlite")
        assert index.get_row("P0001") is not None


class TestRebuildIndex:
    def test_rebuilds_both_indexes_and_reports_counts(self, tmp_path):
        store = MarkdownStore(tmp_path / "data")
        store.write_person(Person(id="P0001", name="Homer Simpson", summary="Works at the plant."))
        result = runner.invoke(app, ["rebuild-index", *_data_dir_args(tmp_path)])
        assert result.exit_code == 0
        assert "1 people indexed" in result.stdout
        assert "1 chunks" in result.stdout

    def test_semantic_only(self, tmp_path):
        store = MarkdownStore(tmp_path / "data")
        store.write_person(Person(id="P0001", name="Homer Simpson", summary="Works at the plant."))
        result = runner.invoke(app, ["rebuild-index", "--semantic-only", *_data_dir_args(tmp_path)])
        assert result.exit_code == 0
        assert "chunks" in result.stdout
        assert "people indexed" not in result.stdout

    def test_deprecated_rebuild_vectors_alias_still_works(self, tmp_path):
        store = MarkdownStore(tmp_path / "data")
        store.write_person(Person(id="P0001", name="Homer Simpson", summary="Works at the plant."))
        result = runner.invoke(app, ["rebuild-vectors", *_data_dir_args(tmp_path)])
        assert result.exit_code == 0
        assert "1 chunks" in result.stdout

    def test_index_status_reports_hybrid_mode(self, tmp_path):
        store = MarkdownStore(tmp_path / "data")
        store.write_person(Person(id="P0001", name="Homer Simpson", summary="Works at the plant."))
        runner.invoke(app, ["rebuild-index", *_data_dir_args(tmp_path)])
        result = runner.invoke(app, ["index-status", *_data_dir_args(tmp_path)])
        assert result.exit_code == 0
        assert "retrieval mode: hybrid" in result.stdout
        assert "Homer" not in result.stdout  # no personal content


class TestGitSnapshot:
    def test_commits_when_data_present(self, tmp_path):
        store = MarkdownStore(tmp_path / "data")
        store.write_person(Person(id="P0001", name="Homer Simpson"))
        result = runner.invoke(app, ["git-snapshot", *_data_dir_args(tmp_path)])
        assert result.exit_code == 0
        assert "committed" in result.stdout

    def test_no_changes_reports_nothing_to_commit(self, tmp_path):
        result = runner.invoke(app, ["git-snapshot", *_data_dir_args(tmp_path)])
        assert result.exit_code == 0
        assert "no changes" in result.stdout


class TestAgentsCommands:
    def test_create_list_revoke_lifecycle(self, tmp_path):
        create_result = runner.invoke(
            app, ["agents", "create", "--name", "briefing-agent", "--role", "read_only", *_data_dir_args(tmp_path)]
        )
        assert create_result.exit_code == 0
        assert "Created agent 'briefing-agent'" in create_result.stdout
        token_line = create_result.stdout.strip().splitlines()[-1]
        assert token_line.startswith("mtk_")

        list_result = runner.invoke(app, ["agents", "list", *_data_dir_args(tmp_path)])
        assert "briefing-agent" in list_result.stdout
        assert "read_only" in list_result.stdout
        assert "active" in list_result.stdout
        assert token_line not in list_result.stdout  # token is never re-printed

        revoke_result = runner.invoke(app, ["agents", "revoke", "briefing-agent", *_data_dir_args(tmp_path)])
        assert revoke_result.exit_code == 0

        list_after = runner.invoke(app, ["agents", "list", *_data_dir_args(tmp_path)])
        assert "revoked at" in list_after.stdout

    def test_create_rejects_invalid_role(self, tmp_path):
        result = runner.invoke(
            app, ["agents", "create", "--name", "x", "--role", "superuser", *_data_dir_args(tmp_path)]
        )
        assert result.exit_code == 1

    def test_revoke_unknown_agent_fails(self, tmp_path):
        result = runner.invoke(app, ["agents", "revoke", "nobody", *_data_dir_args(tmp_path)])
        assert result.exit_code == 1

    def test_list_empty_by_default(self, tmp_path):
        result = runner.invoke(app, ["agents", "list", *_data_dir_args(tmp_path)])
        assert result.exit_code == 0
        assert "no agents registered" in result.stdout


class TestConfigCheck:
    def test_valid_config(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(f"data_dir: {tmp_path / 'data'}\n")
        result = runner.invoke(app, ["config-check", "--config", str(config_path)])
        assert result.exit_code == 0
        assert "config OK" in result.stdout

    def test_invalid_config_exits_nonzero(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text("unknown_top_level_field: 123\n")
        result = runner.invoke(app, ["config-check", "--config", str(config_path)])
        assert result.exit_code == 1
        assert "config invalid" in result.stderr

    def test_missing_config_file_exits_nonzero(self, tmp_path):
        result = runner.invoke(app, ["config-check", "--config", str(tmp_path / "nonexistent.yaml")])
        assert result.exit_code == 1


class TestPlaintextRemoteWarning:
    def test_warns_when_binding_all_interfaces_in_remote_mode(self, caplog):
        with caplog.at_level(logging.WARNING, logger="montauk.cli"):
            _maybe_warn_plaintext_remote("remote", "0.0.0.0")
        assert "TLS-terminating reverse proxy" in caplog.text

    def test_warns_for_ipv6_all_interfaces_too(self, caplog):
        with caplog.at_level(logging.WARNING, logger="montauk.cli"):
            _maybe_warn_plaintext_remote("remote", "::")
        assert "TLS-terminating reverse proxy" in caplog.text

    def test_no_warning_for_loopback(self, caplog):
        with caplog.at_level(logging.WARNING, logger="montauk.cli"):
            _maybe_warn_plaintext_remote("remote", "127.0.0.1")
        assert caplog.text == ""

    def test_no_warning_for_stdio_mode_regardless_of_host(self, caplog):
        with caplog.at_level(logging.WARNING, logger="montauk.cli"):
            _maybe_warn_plaintext_remote("stdio", "0.0.0.0")
        assert caplog.text == ""


class TestBareInvocation:
    def test_bare_montauk_shows_help_and_does_not_start_server(self, tmp_path):
        result = runner.invoke(app, [])
        assert "Usage" in result.stdout
        assert "serve" in result.stdout


def _git(data_dir, *args) -> str:
    import subprocess

    return subprocess.run(
        ["git", *args], cwd=data_dir, capture_output=True, text=True, check=True
    ).stdout


class TestInit:
    def test_requires_a_target(self):
        result = runner.invoke(app, ["init"])
        assert result.exit_code == 1
        assert "--data-dir" in result.stderr

    def test_scaffolds_directory_layout_and_config(self, tmp_path):
        data_dir = tmp_path / "data"
        result = runner.invoke(app, ["init", "--data-dir", str(data_dir)])

        assert result.exit_code == 0
        assert (data_dir / "people").is_dir()
        assert (data_dir / "archive").is_dir()
        assert (data_dir / "config.yaml").exists()
        assert (data_dir / "README.md").exists()
        assert (data_dir / ".gitignore").exists()

    def test_starter_config_points_at_the_data_dir_and_loads(self, tmp_path):
        from montauk.config import load_config

        data_dir = tmp_path / "data"
        runner.invoke(app, ["init", "--data-dir", str(data_dir)])

        cfg = load_config(data_dir / "config.yaml")
        assert cfg.data_dir_path == data_dir.resolve()

    def test_gitignore_excludes_secrets_and_derived_data(self, tmp_path):
        data_dir = tmp_path / "data"
        runner.invoke(app, ["init", "--data-dir", str(data_dir)])

        gitignore = (data_dir / ".gitignore").read_text()
        for excluded in ("auth/", "index/", "logs/"):
            assert excluded in gitignore

    def test_creates_initial_commit_on_main_without_a_remote(self, tmp_path):
        data_dir = tmp_path / "data"
        runner.invoke(app, ["init", "--data-dir", str(data_dir)])

        assert _git(data_dir, "symbolic-ref", "--short", "HEAD").strip() == "main"
        assert _git(data_dir, "log", "--format=%s").strip() == "Initialise Montauk data repository"
        assert _git(data_dir, "remote").strip() == ""

    def test_initial_commit_tracks_scaffold_but_not_auth_or_index(self, tmp_path):
        data_dir = tmp_path / "data"
        runner.invoke(app, ["init", "--data-dir", str(data_dir)])

        tracked = _git(data_dir, "ls-files").split()
        assert "config.yaml" in tracked
        assert "README.md" in tracked
        assert "people/.gitkeep" in tracked
        assert "person-id-sequence.json" in tracked  # canonical ID high-water mark is versioned
        assert not any(p.startswith("auth/") or p.startswith("index/") for p in tracked)

    def test_person_id_sequence_starts_at_zero(self, tmp_path):
        data_dir = tmp_path / "data"
        runner.invoke(app, ["init", "--data-dir", str(data_dir)])
        import json

        assert json.loads((data_dir / "person-id-sequence.json").read_text())["last_allocated"] == 0


class TestMigrateIds:
    def test_converts_name_derived_ids_and_reports(self, tmp_path):
        data_dir = tmp_path / "data"
        store = MarkdownStore(data_dir)
        (store.people_dir / "mike-chen.md").write_text(
            "---\nid: mike-chen\nname: Mike Chen\n---\n\n# Mike Chen\n\n## Interactions\n", encoding="utf-8"
        )
        result = runner.invoke(app, ["migrate-ids", *_data_dir_args(tmp_path)])
        assert result.exit_code == 0
        assert "mike-chen  ->  P0001" in result.stdout
        assert (data_dir / "people" / "P0001.md").exists()
        assert not (data_dir / "people" / "mike-chen.md").exists()

    def test_is_idempotent(self, tmp_path):
        data_dir = tmp_path / "data"
        store = MarkdownStore(data_dir)
        (store.people_dir / "mike-chen.md").write_text(
            "---\nid: mike-chen\nname: Mike Chen\n---\n\n# Mike Chen\n\n## Interactions\n", encoding="utf-8"
        )
        runner.invoke(app, ["migrate-ids", *_data_dir_args(tmp_path)])
        second = runner.invoke(app, ["migrate-ids", *_data_dir_args(tmp_path)])
        assert second.exit_code == 0
        assert "nothing to migrate" in second.stdout

    def test_is_idempotent(self, tmp_path):
        data_dir = tmp_path / "data"
        first = runner.invoke(app, ["init", "--data-dir", str(data_dir)])
        second = runner.invoke(app, ["init", "--data-dir", str(data_dir)])

        assert first.exit_code == 0
        assert second.exit_code == 0
        assert _git(data_dir, "log", "--format=%s").count("Initialise Montauk data repository") == 1

    def test_does_not_clobber_an_existing_config(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "config.yaml").write_text("data_dir: /custom\n", encoding="utf-8")

        runner.invoke(app, ["init", "--data-dir", str(data_dir)])

        assert (data_dir / "config.yaml").read_text() == "data_dir: /custom\n"
