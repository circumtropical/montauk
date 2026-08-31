import datetime as dt

import pytest
from pydantic import ValidationError

from montauk.config import MontaukConfig, load_config


class TestDefaults:
    def test_default_config_is_valid(self):
        config = MontaukConfig()
        assert config.transport.mode == "stdio"
        assert config.git.enabled is True
        assert config.search.semantic_enabled is True
        assert config.logging.level == "INFO"

    def test_daily_snapshot_time_parses(self):
        config = MontaukConfig()
        assert config.daily_snapshot_time_parsed == dt.time(3, 0)

    def test_data_dir_path_expands_and_resolves(self):
        config = MontaukConfig(data_dir="./data")
        assert config.data_dir_path.is_absolute()

    def test_public_url_defaults_to_none(self):
        assert MontaukConfig().transport.public_url is None


class TestPublicUrl:
    def test_accepts_https_url_and_strips_trailing_slash(self):
        config = MontaukConfig(transport={"public_url": "https://montauk.example.com/"})
        assert config.transport.public_url == "https://montauk.example.com"

    def test_accepts_plain_http_url(self):
        config = MontaukConfig(transport={"public_url": "http://10.0.0.4:8765"})
        assert config.transport.public_url == "http://10.0.0.4:8765"

    def test_rejects_url_without_scheme(self):
        with pytest.raises(ValidationError, match="public_url"):
            MontaukConfig(transport={"public_url": "montauk.example.com"})

    def test_rejects_non_http_scheme(self):
        with pytest.raises(ValidationError, match="public_url"):
            MontaukConfig(transport={"public_url": "ftp://montauk.example.com"})

    def test_rejects_url_without_hostname(self):
        with pytest.raises(ValidationError, match="public_url"):
            MontaukConfig(transport={"public_url": "https://"})


class TestLoadConfig:
    def test_loads_yaml_file(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(
            """
data_dir: /var/lib/montauk
transport:
  mode: remote
  host: 0.0.0.0
  port: 9000
git:
  enabled: false
search:
  max_candidates: 10
"""
        )
        config = load_config(path)
        assert config.data_dir == "/var/lib/montauk"
        assert config.transport.mode == "remote"
        assert config.transport.port == 9000
        assert config.git.enabled is False
        assert config.search.max_candidates == 10
        # Fields not overridden keep their defaults.
        assert config.embedding.provider == "local"

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_config(tmp_path / "nonexistent.yaml")

    def test_non_mapping_yaml_raises(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("- just\n- a\n- list\n")
        with pytest.raises(ValueError, match="mapping"):
            load_config(path)

    def test_unknown_top_level_field_rejected(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("data_dir: /tmp/x\nunknown_field: 123\n")
        with pytest.raises(ValidationError):
            load_config(path)

    def test_empty_file_uses_all_defaults(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("")
        config = load_config(path)
        assert config.data_dir == "./data"
