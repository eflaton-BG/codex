from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("agent_secrets.py")
SPEC = importlib.util.spec_from_file_location("standalone_agent_secrets", MODULE_PATH)
assert SPEC is not None
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)

INDEX_USERNAME = MODULE.INDEX_USERNAME
Profile = MODULE.Profile
REDACTED = MODULE.REDACTED
SERVICE_NAME = MODULE.SERVICE_NAME
CredentialStore = MODULE.CredentialStore
oauth_template_profile = MODULE.oauth_template_profile
profile_from_legacy_entries = MODULE.profile_from_legacy_entries
redact_text = MODULE.redact_text
run_cli = MODULE.run_cli
template_profile = MODULE.template_profile


class FakeBackend:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.data[(service_name, username)] = password

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.data.get((service_name, username))

    def delete_password(self, service_name: str, username: str) -> None:
        self.data.pop((service_name, username), None)

    def list_usernames(self, service_name: str) -> list[str]:
        return [
            username
            for (stored_service_name, username), _value in self.data.items()
            if stored_service_name == service_name
        ]


class FakeResponse:
    def __init__(
        self, status_code: int, payload: dict[str, object] | None = None, text: str = ""
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> dict[str, object]:
        if self._payload is None:
            raise json.JSONDecodeError("bad json", "", 0)
        return self._payload


def oauth_profile(*, expires_at: int | None = None) -> Profile:
    profile: Profile = {
        "type": "oauth2",
        "fields": [],
        "oauth": {
            "client_id": "client-id",
            "client_secret": "client-secret",
            "authorization_url": "https://auth.example.com/authorize",
            "token_url": "https://auth.example.com/token",
            "scopes": ["profile", "email"],
            "redirect_uri": "http://127.0.0.1:49152/callback",
            "test_url": "https://api.example.com/health",
            "broker_start_url": "",
            "broker_redeem_url": "",
            "broker_provider": "",
        },
    }
    if expires_at is not None:
        profile["oauth_state"] = {
            "access_token": "expired-token",
            "refresh_token": "refresh-123",
            "token_type": "Bearer",
            "scope": "profile email",
            "expires_at": expires_at,
            "status": "valid",
        }
    return profile


def test_template_profile_uses_private_secret_field() -> None:
    assert template_profile() == {
        "type": "basic",
        "fields": [{"key": "secret", "visibility": "private", "value": ""}],
    }


def test_oauth_template_profile_is_structured() -> None:
    assert oauth_template_profile() == {
        "type": "oauth2",
        "fields": [],
        "oauth": {
            "client_id": "",
            "client_secret": "",
            "authorization_url": "",
            "token_url": "",
            "scopes": [],
            "redirect_uri": "",
            "test_url": "",
            "broker_start_url": "",
            "broker_redeem_url": "",
            "broker_provider": "",
        },
    }


def test_set_allows_incomplete_oauth_profile_drafts(monkeypatch, capsys) -> None:
    backend = FakeBackend()
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "type": "oauth2",
                    "fields": [],
                    "oauth": {
                        "client_id": "",
                        "client_secret": "",
                        "authorization_url": "",
                        "token_url": "",
                        "scopes": [],
                        "redirect_uri": "",
                        "test_url": "",
                        "broker_start_url": "https://n8n.berkshiregrey.com/webhook/agent-secrets-oauth-broker-start",
                        "broker_redeem_url": "https://n8n.berkshiregrey.com/webhook/agent-secrets-oauth-broker-redeem",
                        "broker_provider": "slack",
                    },
                }
            )
        ),
    )

    result = run_cli(["set", "slack/atl", "--stdin-json"], backend=backend)
    captured = capsys.readouterr()

    assert result == 0
    assert "ok" in captured.out
    saved = CredentialStore(backend=backend).get_profile("slack/atl")
    assert saved is not None
    assert saved["type"] == "oauth2"


def test_get_is_redacted_for_oauth_and_private_fields(capsys) -> None:
    backend = FakeBackend()
    profile: Profile = {
        "type": "oauth2",
        "fields": [
            {
                "key": "base_url",
                "visibility": "public",
                "value": "https://api.github.com",
            },
            {"key": "token", "visibility": "private", "value": "ghp_secret"},
        ],
        "oauth": {
            "client_id": "client-id",
            "client_secret": "super-secret-client",
            "authorization_url": "https://auth.example.com/authorize",
            "token_url": "https://auth.example.com/token",
            "scopes": ["repo"],
            "redirect_uri": "",
            "test_url": "https://api.example.com/health",
            "broker_start_url": "",
            "broker_redeem_url": "",
            "broker_provider": "",
        },
        "oauth_state": {
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "status": "valid",
        },
    }
    store = CredentialStore(backend=backend)
    store.set_profile("github/default", profile)
    result = run_cli(["get", "github/default"], backend=backend)
    captured = capsys.readouterr()

    assert result == 0
    assert "ghp_secret" not in captured.out
    assert "super-secret-client" not in captured.out
    assert "access-secret" not in captured.out
    assert "refresh-secret" not in captured.out
    assert REDACTED in captured.out
    assert "https://api.github.com" in captured.out


def test_app_get_requires_explicit_env_gate(monkeypatch, capsys) -> None:
    backend = FakeBackend()
    CredentialStore(backend=backend).set_profile(
        "github/default",
        {
            "type": "basic",
            "fields": [
                {"key": "token", "visibility": "private", "value": "ghp_secret"}
            ],
        },
    )
    monkeypatch.delenv("AGENT_SECRETS_ALLOW_RAW_STDOUT", raising=False)

    result = run_cli(["app-get", "github/default"], backend=backend)
    captured = capsys.readouterr()

    assert result == 1
    assert "raw profile output is disabled" in captured.err


def test_set_from_stdin_json(monkeypatch, capsys) -> None:
    backend = FakeBackend()
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "type": "basic",
                    "fields": [
                        {"key": "token", "visibility": "private", "value": "abc"}
                    ],
                }
            )
        ),
    )

    result = run_cli(["set", "github/default", "--stdin-json"], backend=backend)
    captured = capsys.readouterr()

    assert result == 0
    assert "ok" in captured.out
    assert CredentialStore(backend=backend).get_profile("github/default") is not None


def test_oauth_authorize_persists_token_state(monkeypatch, capsys) -> None:
    backend = FakeBackend()
    CredentialStore(backend=backend).set_profile("github/default", oauth_profile())
    monkeypatch.setattr(MODULE, "_now_epoch", lambda: 1_700_000_000)

    def fake_post(url: str, data: dict[str, str], timeout: int) -> FakeResponse:
        assert url == "https://auth.example.com/token"
        assert data["grant_type"] == "authorization_code"
        assert data["code"] == "code-123"
        assert data["redirect_uri"] == "http://127.0.0.1:49152/callback"
        assert data["code_verifier"] == "verifier-123"
        assert timeout == MODULE.HTTP_TIMEOUT_SECONDS
        return FakeResponse(
            200,
            {
                "access_token": "access-123",
                "refresh_token": "refresh-123",
                "token_type": "Bearer",
                "scope": "profile email",
                "expires_in": 3600,
            },
        )

    monkeypatch.setattr(MODULE.requests, "post", fake_post)
    result = run_cli(
        [
            "oauth-authorize",
            "github/default",
            "--code",
            "code-123",
            "--redirect-uri",
            "http://127.0.0.1:49152/callback",
            "--code-verifier",
            "verifier-123",
        ],
        backend=backend,
    )
    captured = capsys.readouterr()

    assert result == 0
    assert '"status": "valid"' in captured.out

    saved = CredentialStore(backend=backend).get_profile("github/default")
    assert saved is not None
    assert saved["oauth_state"]["access_token"] == "access-123"
    assert saved["oauth_state"]["refresh_token"] == "refresh-123"
    assert saved["oauth_state"]["expires_at"] == 1_700_003_600
    assert saved["oauth_state"]["last_checked_at"] == 1_700_000_000
    assert saved["oauth_state"]["status"] == "valid"


def test_get_access_token_refreshes_when_needed(monkeypatch, capsys) -> None:
    backend = FakeBackend()
    CredentialStore(backend=backend).set_profile(
        "github/default", oauth_profile(expires_at=1_699_999_900)
    )
    monkeypatch.setattr(MODULE, "_now_epoch", lambda: 1_700_000_000)

    def fake_post(url: str, data: dict[str, str], timeout: int) -> FakeResponse:
        assert url == "https://auth.example.com/token"
        assert data["grant_type"] == "refresh_token"
        assert data["refresh_token"] == "refresh-123"
        assert timeout == MODULE.HTTP_TIMEOUT_SECONDS
        return FakeResponse(
            200,
            {
                "access_token": "fresh-token",
                "token_type": "Bearer",
                "expires_in": 1800,
            },
        )

    monkeypatch.setattr(MODULE.requests, "post", fake_post)
    result = run_cli(["get-access-token", "github/default"], backend=backend)
    captured = capsys.readouterr()

    assert result == 0
    assert captured.out.strip() == "fresh-token"

    saved = CredentialStore(backend=backend).get_profile("github/default")
    assert saved is not None
    assert saved["oauth_state"]["access_token"] == "fresh-token"
    assert saved["oauth_state"]["refresh_token"] == "refresh-123"
    assert saved["oauth_state"]["expires_at"] == 1_700_001_800
    assert saved["oauth_state"]["status"] == "valid"


def test_get_access_token_allows_brokered_profile_with_stored_token(capsys) -> None:
    backend = FakeBackend()
    CredentialStore(backend=backend).set_profile(
        "slack/atl",
        {
            "type": "oauth2",
            "fields": [],
            "oauth": {
                "client_id": "",
                "client_secret": "",
                "authorization_url": "",
                "token_url": "",
                "scopes": ["chat:write"],
                "redirect_uri": "",
                "test_url": "https://slack.com/api/auth.test",
                "broker_start_url": "https://n8n.berkshiregrey.com/webhook/agent-secrets-oauth-broker-start",
                "broker_redeem_url": "https://n8n.berkshiregrey.com/webhook/agent-secrets-oauth-broker-redeem",
                "broker_provider": "slack",
            },
            "oauth_state": {
                "access_token": "user-token",
                "token_type": "user",
                "scope": "chat:write",
                "status": "valid",
            },
        },
    )

    result = run_cli(["get-access-token", "slack/atl"], backend=backend)
    captured = capsys.readouterr()

    assert result == 0
    assert captured.out.strip() == "user-token"


def test_oauth_test_allows_brokered_profile_with_stored_token(
    monkeypatch, capsys
) -> None:
    backend = FakeBackend()
    CredentialStore(backend=backend).set_profile(
        "slack/atl",
        {
            "type": "oauth2",
            "fields": [],
            "oauth": {
                "client_id": "",
                "client_secret": "",
                "authorization_url": "",
                "token_url": "",
                "scopes": ["chat:write"],
                "redirect_uri": "",
                "test_url": "https://slack.com/api/auth.test",
                "broker_start_url": "https://n8n.berkshiregrey.com/webhook/agent-secrets-oauth-broker-start",
                "broker_redeem_url": "https://n8n.berkshiregrey.com/webhook/agent-secrets-oauth-broker-redeem",
                "broker_provider": "slack",
            },
            "oauth_state": {
                "access_token": "user-token",
                "token_type": "Bearer",
                "status": "valid",
            },
        },
    )
    monkeypatch.setattr(MODULE, "_now_epoch", lambda: 1_700_000_000)

    def fake_get(url: str, headers: dict[str, str], timeout: int) -> FakeResponse:
        assert url == "https://slack.com/api/auth.test"
        assert headers["Authorization"] == "Bearer user-token"
        assert timeout == MODULE.HTTP_TIMEOUT_SECONDS
        return FakeResponse(200, {"ok": True}, "")

    monkeypatch.setattr(MODULE.requests, "get", fake_get)

    result = run_cli(["oauth-test", "slack/atl"], backend=backend)
    captured = capsys.readouterr()

    assert result == 0
    assert '"status": "valid"' in captured.out


def test_oauth_test_updates_status_and_checked_time(monkeypatch, capsys) -> None:
    backend = FakeBackend()
    profile = oauth_profile(expires_at=1_700_010_000)
    profile["oauth_state"] = {
        "access_token": "access-123",
        "refresh_token": "refresh-123",
        "token_type": "Bearer",
        "expires_at": 1_700_010_000,
        "status": "valid",
    }
    CredentialStore(backend=backend).set_profile("github/default", profile)
    monkeypatch.setattr(MODULE, "_now_epoch", lambda: 1_700_000_000)

    def fake_get(url: str, headers: dict[str, str], timeout: int) -> FakeResponse:
        assert url == "https://api.example.com/health"
        assert headers["Authorization"] == "Bearer access-123"
        assert timeout == MODULE.HTTP_TIMEOUT_SECONDS
        return FakeResponse(200, {}, "")

    monkeypatch.setattr(MODULE.requests, "get", fake_get)
    result = run_cli(["oauth-test", "github/default"], backend=backend)
    captured = capsys.readouterr()

    assert result == 0
    assert '"status": "valid"' in captured.out

    saved = CredentialStore(backend=backend).get_profile("github/default")
    assert saved is not None
    assert saved["oauth_state"]["status"] == "valid"
    assert saved["oauth_state"]["last_checked_at"] == 1_700_000_000
    assert saved["oauth_state"].get("last_error", "") == ""


def test_oauth_test_marks_profile_invalid_on_failed_health_check(
    monkeypatch, capsys
) -> None:
    backend = FakeBackend()
    profile = oauth_profile(expires_at=1_700_010_000)
    profile["oauth_state"] = {
        "access_token": "access-123",
        "refresh_token": "refresh-123",
        "token_type": "Bearer",
        "expires_at": 1_700_010_000,
        "status": "valid",
    }
    CredentialStore(backend=backend).set_profile("github/default", profile)
    monkeypatch.setattr(MODULE, "_now_epoch", lambda: 1_700_000_000)
    monkeypatch.setattr(
        MODULE.requests,
        "get",
        lambda url, headers, timeout: FakeResponse(401, None, "not authorized"),
    )

    result = run_cli(["oauth-test", "github/default"], backend=backend)
    captured = capsys.readouterr()

    assert result == 1
    assert "health check failed with HTTP 401" in captured.err

    saved = CredentialStore(backend=backend).get_profile("github/default")
    assert saved is not None
    assert saved["oauth_state"]["status"] == "invalid"
    assert saved["oauth_state"]["last_checked_at"] == 1_700_000_000
    assert "HTTP 401" in saved["oauth_state"]["last_error"]


def test_brokered_get_access_token_refreshes_via_broker_redeem(
    monkeypatch, capsys
) -> None:
    backend = FakeBackend()
    CredentialStore(backend=backend).set_profile(
        "slack/atl",
        {
            "type": "oauth2",
            "fields": [],
            "oauth": {
                "client_id": "",
                "client_secret": "",
                "authorization_url": "",
                "token_url": "",
                "scopes": ["chat:write"],
                "redirect_uri": "",
                "test_url": "",
                "broker_start_url": "",
                "broker_redeem_url": "",
                "broker_provider": "slack",
            },
            "oauth_state": {
                "access_token": "expired-user-token",
                "refresh_token": "refresh-123",
                "token_type": "Bearer",
                "scope": "chat:write",
                "expires_at": 1,
                "status": "valid",
            },
        },
    )
    monkeypatch.setattr(MODULE, "_now_epoch", lambda: 1_700_000_000)

    def fake_post(url: str, json: dict[str, str], timeout: int) -> FakeResponse:
        assert (
            url
            == "https://n8n.berkshiregrey.com/webhook/agent-secrets-oauth-broker-redeem"
        )
        assert json == {"provider": "slack", "refresh_token": "refresh-123"}
        assert timeout == MODULE.HTTP_TIMEOUT_SECONDS
        return FakeResponse(
            200,
            {
                "ok": True,
                "provider": "slack",
                "token_payload": {
                    "access_token": "fresh-user-token",
                    "refresh_token": "refresh-456",
                    "token_type": "Bearer",
                    "scope": "chat:write",
                    "expires_at": 1_700_001_800,
                },
            },
        )

    monkeypatch.setattr(MODULE.requests, "post", fake_post)
    result = run_cli(["get-access-token", "slack/atl"], backend=backend)
    captured = capsys.readouterr()

    assert result == 0
    assert captured.out.strip() == "fresh-user-token"

    saved = CredentialStore(backend=backend).get_profile("slack/atl")
    assert saved is not None
    assert saved["oauth_state"]["access_token"] == "fresh-user-token"
    assert saved["oauth_state"]["refresh_token"] == "refresh-456"
    assert saved["oauth_state"]["expires_at"] == 1_700_001_800
    assert saved["oauth_state"]["status"] == "valid"


def test_run_refreshes_github_oauth_access_token_before_env_injection(
    monkeypatch, capfd
) -> None:
    backend = FakeBackend()
    CredentialStore(backend=backend).set_profile(
        "github/atl",
        {
            "type": "oauth2",
            "fields": [],
            "oauth": {
                "client_id": "",
                "client_secret": "",
                "authorization_url": "https://github.com/login/oauth/authorize",
                "token_url": "https://github.com/login/oauth/access_token",
                "scopes": ["read:user"],
                "redirect_uri": "https://n8n.berkshiregrey.com/webhook/agent-secrets-github-oauth-callback",
                "test_url": "https://api.github.com/user",
                "broker_start_url": "https://n8n.berkshiregrey.com/webhook/agent-secrets-oauth-broker-start",
                "broker_redeem_url": "https://n8n.berkshiregrey.com/webhook/agent-secrets-oauth-broker-redeem",
                "broker_provider": "github",
            },
            "oauth_state": {
                "access_token": "expired-token",
                "refresh_token": "refresh-123",
                "token_type": "bearer",
                "expires_at": 1_699_999_900,
                "status": "valid",
            },
        },
    )
    monkeypatch.setattr(MODULE, "_now_epoch", lambda: 1_700_000_000)

    def fake_post(url: str, json: dict[str, str], timeout: int) -> FakeResponse:
        assert (
            url
            == "https://n8n.berkshiregrey.com/webhook/agent-secrets-oauth-broker-redeem"
        )
        assert json == {"provider": "github", "refresh_token": "refresh-123"}
        assert timeout == MODULE.HTTP_TIMEOUT_SECONDS
        return FakeResponse(
            200,
            {
                "ok": True,
                "provider": "github",
                "token_payload": {
                    "access_token": "fresh-token",
                    "token_type": "bearer",
                    "expires_at": 1_700_001_800,
                },
            },
        )

    monkeypatch.setattr(MODULE.requests, "post", fake_post)
    capfd.readouterr()

    result = run_cli(
        [
            "run",
            "--profile",
            "github/atl",
            "--env",
            "GITHUB_TOKEN=oauth_state.access_token",
            "--",
            sys.executable,
            "-c",
            "import os; print(os.environ['GITHUB_TOKEN']); print('done')",
        ],
        backend=backend,
    )
    captured = capfd.readouterr()

    assert result == 0
    assert "fresh-token" not in captured.out
    assert REDACTED in captured.out
    assert "done" in captured.out

    saved = CredentialStore(backend=backend).get_profile("github/atl")
    assert saved is not None
    assert saved["oauth_state"]["access_token"] == "fresh-token"
    assert saved["oauth_state"]["refresh_token"] == "refresh-123"
    assert saved["oauth_state"]["expires_at"] == 1_700_001_800
    assert saved["oauth_state"]["status"] == "valid"


def test_run_surfaces_brokered_refresh_failure(monkeypatch, capsys) -> None:
    backend = FakeBackend()
    CredentialStore(backend=backend).set_profile(
        "slack/atl",
        {
            "type": "oauth2",
            "fields": [],
            "oauth": {
                "client_id": "",
                "client_secret": "",
                "authorization_url": "",
                "token_url": "",
                "scopes": ["chat:write"],
                "redirect_uri": "",
                "test_url": "https://slack.com/api/auth.test",
                "broker_start_url": "https://n8n.berkshiregrey.com/webhook/agent-secrets-oauth-broker-start",
                "broker_redeem_url": "https://n8n.berkshiregrey.com/webhook/agent-secrets-oauth-broker-redeem",
                "broker_provider": "slack",
            },
            "oauth_state": {
                "access_token": "expired-user-token",
                "refresh_token": "refresh-123",
                "token_type": "Bearer",
                "scope": "chat:write",
                "expires_at": 1,
                "status": "valid",
            },
        },
    )
    monkeypatch.setattr(MODULE, "_now_epoch", lambda: 1_700_000_000)

    def fake_post(url: str, json: dict[str, str], timeout: int) -> FakeResponse:
        assert (
            url
            == "https://n8n.berkshiregrey.com/webhook/agent-secrets-oauth-broker-redeem"
        )
        assert json == {"provider": "slack", "refresh_token": "refresh-123"}
        assert timeout == MODULE.HTTP_TIMEOUT_SECONDS
        return FakeResponse(400, {"ok": False}, "refresh failed")

    monkeypatch.setattr(MODULE.requests, "post", fake_post)

    result = run_cli(
        [
            "run",
            "--profile",
            "slack/atl",
            "--env",
            "SLACK_TOKEN=oauth_state.access_token",
            "--",
            "true",
        ],
        backend=backend,
    )
    captured = capsys.readouterr()

    assert result == 1
    assert "oauth broker refresh failed with HTTP 400" in captured.err


def test_get_access_token_surfaces_broker_refresh_json_error(
    monkeypatch, capsys
) -> None:
    backend = FakeBackend()
    CredentialStore(backend=backend).set_profile(
        "github/atl",
        {
            "type": "oauth2",
            "fields": [],
            "oauth": {
                "client_id": "",
                "client_secret": "",
                "authorization_url": "https://github.com/login/oauth/authorize",
                "token_url": "https://github.com/login/oauth/access_token",
                "scopes": ["read:user"],
                "redirect_uri": "https://n8n.berkshiregrey.com/webhook/agent-secrets-github-oauth-callback",
                "test_url": "https://api.github.com/user",
                "broker_start_url": "https://n8n.berkshiregrey.com/webhook/agent-secrets-oauth-broker-start",
                "broker_redeem_url": "https://n8n.berkshiregrey.com/webhook/agent-secrets-oauth-broker-redeem",
                "broker_provider": "github",
            },
            "oauth_state": {
                "access_token": "expired-token",
                "refresh_token": "refresh-123",
                "token_type": "bearer",
                "expires_at": 1,
                "status": "valid",
            },
        },
    )

    def fake_post(url: str, json: dict[str, str], timeout: int) -> FakeResponse:
        assert (
            url
            == "https://n8n.berkshiregrey.com/webhook/agent-secrets-oauth-broker-redeem"
        )
        assert json == {"provider": "github", "refresh_token": "refresh-123"}
        assert timeout == MODULE.HTTP_TIMEOUT_SECONDS
        return FakeResponse(
            200,
            {
                "ok": False,
                "provider": "github",
                "error": "bad_refresh_token",
                "error_description": "The refresh token passed is incorrect or expired.",
            },
        )

    monkeypatch.setattr(MODULE.requests, "post", fake_post)
    result = run_cli(["get-access-token", "github/atl"], backend=backend)
    captured = capsys.readouterr()

    assert result == 1
    assert (
        "oauth broker refresh failed: bad_refresh_token: The refresh token passed is incorrect or expired."
        in captured.err
    )


def test_get_access_token_reloads_profile_inside_refresh_lock(
    monkeypatch, capsys
) -> None:
    backend = FakeBackend()
    store = CredentialStore(backend=backend)
    store.set_profile(
        "github/atl",
        {
            "type": "oauth2",
            "fields": [],
            "oauth": {
                "client_id": "",
                "client_secret": "",
                "authorization_url": "https://github.com/login/oauth/authorize",
                "token_url": "https://github.com/login/oauth/access_token",
                "scopes": ["read:user"],
                "redirect_uri": "https://n8n.berkshiregrey.com/webhook/agent-secrets-github-oauth-callback",
                "test_url": "https://api.github.com/user",
                "broker_start_url": "https://n8n.berkshiregrey.com/webhook/agent-secrets-oauth-broker-start",
                "broker_redeem_url": "https://n8n.berkshiregrey.com/webhook/agent-secrets-oauth-broker-redeem",
                "broker_provider": "github",
            },
            "oauth_state": {
                "access_token": "fresh-token-from-other-process",
                "refresh_token": "refresh-456",
                "token_type": "bearer",
                "expires_at": 1_700_001_800,
                "status": "valid",
            },
        },
    )
    stale_profile = {
        "type": "oauth2",
        "fields": [],
        "oauth": {
            "client_id": "",
            "client_secret": "",
            "authorization_url": "https://github.com/login/oauth/authorize",
            "token_url": "https://github.com/login/oauth/access_token",
            "scopes": ["read:user"],
            "redirect_uri": "https://n8n.berkshiregrey.com/webhook/agent-secrets-github-oauth-callback",
            "test_url": "https://api.github.com/user",
            "broker_start_url": "https://n8n.berkshiregrey.com/webhook/agent-secrets-oauth-broker-start",
            "broker_redeem_url": "https://n8n.berkshiregrey.com/webhook/agent-secrets-oauth-broker-redeem",
            "broker_provider": "github",
        },
        "oauth_state": {
            "access_token": "expired-token",
            "refresh_token": "refresh-123",
            "token_type": "bearer",
            "expires_at": 1,
            "status": "valid",
        },
    }
    monkeypatch.setattr(MODULE, "_now_epoch", lambda: 1_700_000_000)

    def fail_post(*args, **kwargs):
        raise AssertionError(
            "refresh should not run when a fresh profile is already stored"
        )

    monkeypatch.setattr(MODULE.requests, "post", fail_post)
    profile_out, access_token = MODULE.ensure_fresh_access_token(
        store, "github/atl", stale_profile
    )

    assert access_token == "fresh-token-from-other-process"
    assert profile_out["oauth_state"]["refresh_token"] == "refresh-456"


def test_validate_profile_infers_slack_broker_defaults() -> None:
    profile = MODULE.validate_profile(
        {
            "type": "oauth2",
            "fields": [],
            "oauth": {
                "client_id": "",
                "client_secret": "",
                "authorization_url": "",
                "token_url": "",
                "scopes": ["chat:write"],
                "redirect_uri": "",
                "test_url": "",
                "broker_start_url": "",
                "broker_redeem_url": "",
                "broker_provider": "slack",
            },
        }
    )

    assert (
        profile["oauth"]["broker_start_url"]
        == "https://n8n.berkshiregrey.com/webhook/agent-secrets-oauth-broker-start"
    )
    assert (
        profile["oauth"]["broker_redeem_url"]
        == "https://n8n.berkshiregrey.com/webhook/agent-secrets-oauth-broker-redeem"
    )
    assert profile["oauth"]["test_url"] == "https://slack.com/api/auth.test"


def test_validate_profile_infers_github_broker_defaults() -> None:
    profile = MODULE.validate_profile(
        {
            "type": "oauth2",
            "fields": [],
            "oauth": {
                "client_id": "",
                "client_secret": "",
                "authorization_url": "",
                "token_url": "",
                "scopes": [],
                "redirect_uri": "",
                "test_url": "",
                "broker_start_url": "",
                "broker_redeem_url": "",
                "broker_provider": "github",
            },
        }
    )

    assert (
        profile["oauth"]["broker_start_url"]
        == "https://n8n.berkshiregrey.com/webhook/agent-secrets-oauth-broker-start"
    )
    assert (
        profile["oauth"]["broker_redeem_url"]
        == "https://n8n.berkshiregrey.com/webhook/agent-secrets-oauth-broker-redeem"
    )
    assert profile["oauth"]["test_url"] == "https://api.github.com/user"


def test_validate_profile_infers_box_broker_defaults() -> None:
    profile = MODULE.validate_profile(
        {
            "type": "oauth2",
            "fields": [],
            "oauth": {
                "client_id": "",
                "client_secret": "",
                "authorization_url": "",
                "token_url": "",
                "scopes": [],
                "redirect_uri": "",
                "test_url": "",
                "broker_start_url": "",
                "broker_redeem_url": "",
                "broker_provider": "box",
            },
        }
    )

    assert (
        profile["oauth"]["broker_start_url"]
        == "https://n8n.berkshiregrey.com/webhook/agent-secrets-oauth-broker-start"
    )
    assert (
        profile["oauth"]["broker_redeem_url"]
        == "https://n8n.berkshiregrey.com/webhook/agent-secrets-oauth-broker-redeem"
    )
    assert profile["oauth"]["test_url"] == "https://api.box.com/2.0/users/me"


def test_oauth_test_normalizes_lowercase_bearer_token_type(monkeypatch) -> None:
    backend = FakeBackend()
    store = CredentialStore(backend=backend)
    profile = MODULE.validate_profile(
        {
            "type": "oauth2",
            "fields": [],
            "oauth": {
                "client_id": "",
                "client_secret": "",
                "authorization_url": "",
                "token_url": "",
                "scopes": [],
                "redirect_uri": "",
                "test_url": "https://api.box.com/2.0/users/me",
                "broker_start_url": "https://n8n.berkshiregrey.com/webhook/agent-secrets-oauth-broker-start",
                "broker_redeem_url": "https://n8n.berkshiregrey.com/webhook/agent-secrets-oauth-broker-redeem",
                "broker_provider": "box",
            },
            "oauth_state": {
                "access_token": "box-access-token",
                "refresh_token": "box-refresh-token",
                "token_type": "bearer",
                "expires_at": 1_700_001_800,
                "status": "valid",
            },
        }
    )
    store.set_profile("box/atl", profile)
    monkeypatch.setattr(MODULE, "_now_epoch", lambda: 1_700_000_000)

    def fake_get(url: str, headers: dict[str, str], timeout: int) -> FakeResponse:
        assert url == "https://api.box.com/2.0/users/me"
        assert headers == {"Authorization": "Bearer box-access-token"}
        assert timeout == MODULE.HTTP_TIMEOUT_SECONDS
        return FakeResponse(200, {"type": "user", "id": "123"})

    monkeypatch.setattr(MODULE.requests, "get", fake_get)

    updated, result = MODULE.test_oauth_token(store, "box/atl", profile)

    assert result["status"] == "valid"
    assert updated["oauth_state"]["token_type"] == "bearer"


def test_run_injects_env_and_redacts_child_output(capfd) -> None:
    backend = FakeBackend()
    store = CredentialStore(backend=backend)
    secret = "top-secret-token"
    store.set_profile(
        "github/default",
        {
            "type": "basic",
            "fields": [{"key": "token", "visibility": "private", "value": secret}],
        },
    )
    capfd.readouterr()

    result = run_cli(
        [
            "run",
            "--profile",
            "github/default",
            "--env",
            "GITHUB_TOKEN=private.token",
            "--",
            sys.executable,
            "-c",
            "import os; print(os.environ['GITHUB_TOKEN']); print('done')",
        ],
        backend=backend,
    )
    captured = capfd.readouterr()

    assert result == 0
    assert secret not in captured.out
    assert REDACTED in captured.out
    assert "done" in captured.out


def test_run_rejects_invalid_selector(capsys) -> None:
    backend = FakeBackend()
    CredentialStore(backend=backend).set_profile(
        "github/default",
        {
            "type": "basic",
            "fields": [{"key": "token", "visibility": "private", "value": "abc"}],
        },
    )

    result = run_cli(
        [
            "run",
            "--profile",
            "github/default",
            "--env",
            "GITHUB_TOKEN=private.missing",
            "--",
            "true",
        ],
        backend=backend,
    )
    captured = capsys.readouterr()

    assert result == 1
    assert "invalid mapping selector" in captured.err


def test_run_supports_oauth_selector_and_redacts_child_output(capfd) -> None:
    backend = FakeBackend()
    CredentialStore(backend=backend).set_profile(
        "slack/atl",
        {
            "type": "oauth2",
            "fields": [],
            "oauth": {
                "client_id": "",
                "client_secret": "",
                "authorization_url": "",
                "token_url": "",
                "scopes": ["chat:write"],
                "redirect_uri": "",
                "test_url": "https://slack.com/api/auth.test",
                "broker_start_url": "https://n8n.berkshiregrey.com/webhook/agent-secrets-oauth-broker-start",
                "broker_redeem_url": "https://n8n.berkshiregrey.com/webhook/agent-secrets-oauth-broker-redeem",
                "broker_provider": "slack",
            },
            "oauth_state": {
                "access_token": "xoxp-secret-token",
                "token_type": "user",
                "status": "valid",
            },
        },
    )
    capfd.readouterr()

    result = run_cli(
        [
            "run",
            "--profile",
            "slack/atl",
            "--env",
            "SLACK_TOKEN=oauth_state.access_token",
            "--",
            sys.executable,
            "-c",
            "import os; print(os.environ['SLACK_TOKEN']); print('done')",
        ],
        backend=backend,
    )
    captured = capfd.readouterr()

    assert result == 0
    assert "xoxp-secret-token" not in captured.out
    assert REDACTED in captured.out
    assert "done" in captured.out


def test_redact_text_replaces_longer_secret_first() -> None:
    text = "abc123 abc"
    assert redact_text(text, ["abc", "abc123"]) == f"{REDACTED} {REDACTED}"


def test_store_list_falls_back_to_backend_usernames() -> None:
    backend = FakeBackend()
    backend.set_password(
        SERVICE_NAME,
        "legacy/default",
        json.dumps(
            {"fields": [{"key": "token", "visibility": "private", "value": "abc"}]}
        ),
    )
    backend.set_password(SERVICE_NAME, INDEX_USERNAME, json.dumps([]))

    assert CredentialStore(backend=backend).list_keys() == ["legacy/default"]


def test_profile_from_legacy_entries_transforms_shape() -> None:
    assert profile_from_legacy_entries(
        [{"key": "token", "visibility": "private", "value": "abc"}]
    ) == {
        "type": "basic",
        "fields": [{"key": "token", "visibility": "private", "value": "abc"}],
    }
