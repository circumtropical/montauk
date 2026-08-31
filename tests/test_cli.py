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
        store.write_person(Person(id="homer-simpson", name="Homer Simpson"))
        result = runner.invoke(app, ["status", *_data_dir_args(tmp_path)])
        assert result.exit_code == 0
        assert "valid people: 1" in result.stdout


class TestRebuildIndex:
    def test_rebuilds_and_reports_count(self, tmp_path):
        store = MarkdownStore(tmp_path / "data")
        store.write_person(Person(id="homer-simpson", name="Homer Simpson"))
        result = runner.invoke(app, ["rebuild-index", *_data_dir_args(tmp_path)])
        assert result.exit_code == 0
        assert "1 people indexed" in result.stdout

        from montauk.sqlite_index import SqliteIndex

        index = SqliteIndex(tmp_path / "data" / "index" / "relationships.sqlite")
        assert index.get_row("homer-simpson") is not None


class TestRebuildVectors:
    def test_rebuilds_and_reports_chunk_count(self, tmp_path):
        store = MarkdownStore(tmp_path / "data")
        store.write_person(Person(id="homer-simpson", name="Homer Simpson", summary="Works at the plant."))
        result = runner.invoke(app, ["rebuild-vectors", *_data_dir_args(tmp_path)])
        assert result.exit_code == 0
        assert "1 chunks" in result.stdout


class TestGitSnapshot:
    def test_commits_when_data_present(self, tmp_path):
        store = MarkdownStore(tmp_path / "data")
        store.write_person(Person(id="homer-simpson", name="Homer Simpson"))
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
