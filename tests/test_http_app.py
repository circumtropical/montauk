from starlette.testclient import TestClient

from montauk.auth import CredentialStore
from montauk.http_app import build_http_app
from montauk.server import create_server


def _client(tmp_path, *, with_auth: bool) -> tuple[TestClient, CredentialStore | None, str | None]:
    credential_store = None
    token = None
    if with_auth:
        credential_store = CredentialStore(tmp_path / "auth.sqlite")
        _record, token = credential_store.create_agent("agent", "read_write")
    server = create_server()
    app = build_http_app(server, credential_store=credential_store, host="127.0.0.1")
    return TestClient(app), credential_store, token


_INITIALIZE_BODY = {"jsonrpc": "2.0", "method": "initialize", "id": 1}


class TestBearerAuthMiddleware:
    def test_no_credential_store_lets_requests_through(self, tmp_path):
        client, _cred, _token = _client(tmp_path, with_auth=False)
        with client:
            response = client.post("/mcp", json=_INITIALIZE_BODY)
        assert response.status_code != 401

    def test_missing_authorization_header_is_rejected(self, tmp_path):
        client, _cred, _token = _client(tmp_path, with_auth=True)
        with client:
            response = client.post("/mcp", json=_INITIALIZE_BODY)
        assert response.status_code == 401

    def test_invalid_token_is_rejected(self, tmp_path):
        client, _cred, _token = _client(tmp_path, with_auth=True)
        with client:
            response = client.post(
                "/mcp", json=_INITIALIZE_BODY, headers={"Authorization": "Bearer not-a-real-token"}
            )
        assert response.status_code == 401

    def test_revoked_token_is_rejected(self, tmp_path):
        client, credential_store, token = _client(tmp_path, with_auth=True)
        agents = credential_store.list_agents()
        credential_store.revoke_agent(agents[0].agent_id)
        with client:
            response = client.post("/mcp", json=_INITIALIZE_BODY, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401

    def test_malformed_authorization_scheme_is_rejected(self, tmp_path):
        client, _cred, token = _client(tmp_path, with_auth=True)
        with client:
            response = client.post("/mcp", json=_INITIALIZE_BODY, headers={"Authorization": f"Basic {token}"})
        assert response.status_code == 401

    def test_fails_closed_with_zero_registered_agents(self, tmp_path):
        # A freshly-created, empty CredentialStore (no agents ever
        # created) must still reject everything -- auth is deny-by-
        # default, not allow-until-a-credential-exists.
        credential_store = CredentialStore(tmp_path / "auth.sqlite")
        assert credential_store.list_agents() == []
        server = create_server()
        app = build_http_app(server, credential_store=credential_store, host="127.0.0.1")
        with TestClient(app) as client:
            response = client.post("/mcp", json=_INITIALIZE_BODY)
        assert response.status_code == 401

    def test_valid_token_is_not_rejected_by_the_auth_layer(self, tmp_path):
        client, _cred, token = _client(tmp_path, with_auth=True)
        with client:
            response = client.post(
                "/mcp",
                json=_INITIALIZE_BODY,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"},
            )
        # A valid credential must clear our middleware; whatever happens
        # deeper in MCP protocol dispatch (session/host-header handling)
        # is the SDK's own concern, not this test's.
        assert response.status_code != 401
