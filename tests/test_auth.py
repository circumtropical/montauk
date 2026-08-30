import pytest

from montauk.auth import (
    CredentialStore,
    extract_bearer_token,
    require_write,
    resolve_http_identity,
    resolve_stdio_identity,
)
from montauk.errors import PermissionDeniedError


def _store(tmp_path) -> CredentialStore:
    return CredentialStore(tmp_path / "auth" / "credentials.sqlite")


class TestCreateAgent:
    def test_creates_agent_and_returns_raw_token_once(self, tmp_path):
        store = _store(tmp_path)
        record, token = store.create_agent("briefing-agent", "read_only")
        assert record.agent_id == "briefing-agent"
        assert record.role == "read_only"
        assert record.revoked is False
        assert token.startswith("mtk_")
        assert token[:12] == record.token_prefix

    def test_raw_token_is_never_persisted(self, tmp_path):
        store = _store(tmp_path)
        _record, token = store.create_agent("agent", "read_write")
        raw_row = store._conn.execute("SELECT token_hash FROM agents").fetchone()
        assert token not in raw_row["token_hash"]
        assert len(raw_row["token_hash"]) == 64  # sha256 hex digest

    def test_duplicate_name_gets_suffixed_agent_id(self, tmp_path):
        store = _store(tmp_path)
        r1, _ = store.create_agent("briefing-agent", "read_only")
        r2, _ = store.create_agent("briefing-agent", "read_only")
        assert r1.agent_id == "briefing-agent"
        assert r2.agent_id == "briefing-agent-2"


class TestVerifyToken:
    def test_valid_token_resolves_identity(self, tmp_path):
        store = _store(tmp_path)
        record, token = store.create_agent("writer", "read_write")
        identity = store.verify_token(token)
        assert identity is not None
        assert identity.agent_id == record.agent_id
        assert identity.role == "read_write"

    def test_unknown_token_returns_none(self, tmp_path):
        store = _store(tmp_path)
        assert store.verify_token("mtk_totally-made-up") is None

    def test_empty_token_returns_none(self, tmp_path):
        store = _store(tmp_path)
        assert store.verify_token("") is None

    def test_revoked_token_returns_none(self, tmp_path):
        store = _store(tmp_path)
        record, token = store.create_agent("writer", "read_write")
        store.revoke_agent(record.agent_id)
        assert store.verify_token(token) is None


class TestRevokeAgent:
    def test_revoke_returns_true_once(self, tmp_path):
        store = _store(tmp_path)
        record, _token = store.create_agent("writer", "read_write")
        assert store.revoke_agent(record.agent_id) is True
        assert store.revoke_agent(record.agent_id) is False  # already revoked

    def test_revoke_unknown_agent_returns_false(self, tmp_path):
        store = _store(tmp_path)
        assert store.revoke_agent("nobody") is False


class TestListAgents:
    def test_lists_agents_without_exposing_raw_tokens(self, tmp_path):
        store = _store(tmp_path)
        store.create_agent("reader", "read_only")
        store.create_agent("writer", "read_write")
        records = store.list_agents()
        assert {r.agent_id for r in records} == {"reader", "writer"}
        for r in records:
            assert not hasattr(r, "token")
            assert not hasattr(r, "token_hash")


class TestExtractBearerToken:
    @pytest.mark.parametrize(
        "headers,expected",
        [
            ({"Authorization": "Bearer mtk_abc123"}, "mtk_abc123"),
            ({"authorization": "Bearer mtk_abc123"}, "mtk_abc123"),
            ({"AUTHORIZATION": "bearer mtk_abc123"}, "mtk_abc123"),
            ({}, None),
            (None, None),
            ({"Authorization": "Basic dXNlcjpwYXNz"}, None),
            ({"Authorization": "Bearer "}, None),
            ({"X-Other": "irrelevant"}, None),
        ],
    )
    def test_extraction(self, headers, expected):
        assert extract_bearer_token(headers) == expected


class TestResolveIdentity:
    def test_resolve_http_identity_valid(self, tmp_path):
        store = _store(tmp_path)
        record, token = store.create_agent("writer", "read_write")
        identity = resolve_http_identity(store, {"Authorization": f"Bearer {token}"})
        assert identity.agent_id == record.agent_id

    def test_resolve_http_identity_missing_header(self, tmp_path):
        store = _store(tmp_path)
        assert resolve_http_identity(store, {}) is None
        assert resolve_http_identity(store, None) is None

    def test_resolve_stdio_identity_valid(self, tmp_path):
        store = _store(tmp_path)
        record, token = store.create_agent("local-agent", "read_write")
        identity = resolve_stdio_identity(store, token)
        assert identity.agent_id == record.agent_id

    def test_resolve_stdio_identity_none_token(self, tmp_path):
        store = _store(tmp_path)
        assert resolve_stdio_identity(store, None) is None


class TestRequireWrite:
    def test_read_write_identity_passes(self, tmp_path):
        store = _store(tmp_path)
        _record, token = store.create_agent("writer", "read_write")
        identity = store.verify_token(token)
        require_write(identity)  # does not raise

    def test_read_only_identity_denied(self, tmp_path):
        store = _store(tmp_path)
        _record, token = store.create_agent("reader", "read_only")
        identity = store.verify_token(token)
        with pytest.raises(PermissionDeniedError, match="PERMISSION_DENIED"):
            require_write(identity)

    def test_no_identity_denied(self):
        with pytest.raises(PermissionDeniedError, match="PERMISSION_DENIED"):
            require_write(None)
