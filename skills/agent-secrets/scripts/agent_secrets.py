from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Literal, Protocol, TypedDict, cast

import requests


KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]*/[a-z0-9][a-z0-9_.-]*$")
SERVICE_NAME = "agent_secrets"
INDEX_USERNAME = "__profile_index__"
REDACTED = "***"
ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 60
HTTP_TIMEOUT_SECONDS = 20
DEFAULT_BROKER_START_URL = (
    "https://n8n.berkshiregrey.com/webhook/agent-secrets-oauth-broker-start"
)
DEFAULT_BROKER_REDEEM_URL = (
    "https://n8n.berkshiregrey.com/webhook/agent-secrets-oauth-broker-redeem"
)
SLACK_BROKER_PROVIDER = "slack"
GITHUB_BROKER_PROVIDER = "github"
BOX_BROKER_PROVIDER = "box"
SLACK_TEST_URL = "https://slack.com/api/auth.test"
GITHUB_TEST_URL = "https://api.github.com/user"
BOX_TEST_URL = "https://api.box.com/2.0/users/me"
BROKER_DEFAULT_TEST_URLS = {
    SLACK_BROKER_PROVIDER: SLACK_TEST_URL,
    GITHUB_BROKER_PROVIDER: GITHUB_TEST_URL,
    BOX_BROKER_PROVIDER: BOX_TEST_URL,
}
SENSITIVE_EXACT_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "client_secret",
    "id_token",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "token",
}
SENSITIVE_SUFFIXES = ("_secret", "_token", "_password", "_api_key", "_private_key")

Visibility = Literal["public", "private"]
ProfileType = Literal["basic", "oauth2"]
OAuthStatus = Literal["valid", "invalid"]


class Field(TypedDict):
    key: str
    visibility: Visibility
    value: object


class OAuthConfig(TypedDict):
    client_id: str
    client_secret: str
    authorization_url: str
    token_url: str
    scopes: list[str]
    redirect_uri: str
    test_url: str
    broker_start_url: str
    broker_redeem_url: str
    broker_provider: str


class OAuthState(TypedDict, total=False):
    access_token: str
    refresh_token: str
    token_type: str
    scope: str
    expires_at: int
    last_checked_at: int
    status: OAuthStatus
    last_error: str


class BaseProfile(TypedDict):
    type: ProfileType
    fields: list[Field]


class Profile(BaseProfile, total=False):
    oauth: OAuthConfig
    oauth_state: OAuthState


class StoreBackend(Protocol):
    def set_password(self, service_name: str, username: str, password: str) -> None: ...

    def get_password(self, service_name: str, username: str) -> str | None: ...

    def delete_password(self, service_name: str, username: str) -> None: ...


class KeyringBackend:
    def __init__(self) -> None:
        import keyring

        self._keyring = keyring

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self._keyring.set_password(service_name, username, password)

    def get_password(self, service_name: str, username: str) -> str | None:
        return self._keyring.get_password(service_name, username)

    def delete_password(self, service_name: str, username: str) -> None:
        self._keyring.delete_password(service_name, username)

    def list_usernames(self, service_name: str) -> list[str]:
        keyring_impl = cast(Any, self._keyring.get_keyring())
        if not hasattr(keyring_impl, "get_preferred_collection"):
            return []
        try:
            collection = keyring_impl.get_preferred_collection()
            items = collection.search_items({"service": service_name})
        except Exception:
            return []

        usernames: list[str] = []
        try:
            for item in items:
                attributes = item.get_attributes()
                username = attributes.get("username")
                if isinstance(username, str):
                    usernames.append(username)
        except Exception:
            return []
        return usernames


def validate_credential_key(credential_key: str) -> str:
    if not KEY_PATTERN.match(credential_key):
        raise ValueError("credential key must match <namespace>/<name>")
    return credential_key


def _now_epoch() -> int:
    return int(time.time())


def _runtime_dir() -> Path:
    return Path("/tmp")


def _credential_lock_path(credential_key: str) -> Path:
    safe_key = credential_key.replace("/", "__")
    return _runtime_dir() / f"agent_secrets_refresh_{safe_key}.lock"


@contextlib.contextmanager
def _credential_refresh_lock(credential_key: str):
    lock_path = _credential_lock_path(credential_key)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _validate_field(raw: object, index: int) -> Field:
    if not isinstance(raw, dict):
        raise ValueError(f"field at index {index} must be an object")
    key = raw.get("key")
    visibility = raw.get("visibility")
    if not isinstance(key, str) or not key.strip():
        raise ValueError(f"field at index {index} key must be a non-empty string")
    if visibility not in ("public", "private"):
        raise ValueError(f"field at index {index} visibility must be public/private")
    return {
        "key": key.strip(),
        "visibility": cast(Visibility, visibility),
        "value": raw.get("value"),
    }


def _normalize_scopes(raw: object) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        items = raw.replace("\n", " ").split(" ")
        return [item.strip() for item in items if item.strip()]
    if not isinstance(raw, list):
        raise ValueError("oauth.scopes must be a list or string")
    scopes: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("oauth.scopes entries must be non-empty strings")
        scopes.append(item.strip())
    return scopes


def _validate_required_string(raw: object, *, field_name: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return raw.strip()


def _validate_draft_string(raw: object, *, field_name: str) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValueError(f"{field_name} must be a string")
    return raw.strip()


def _validate_optional_string(raw: object, *, field_name: str) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"{field_name} must be a string")
    value = raw.strip()
    return value or None


def _validate_optional_int(raw: object, *, field_name: str) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise ValueError(f"{field_name} must be an integer")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float) and raw.is_integer():
        return int(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            return int(raw.strip())
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an integer") from exc
    raise ValueError(f"{field_name} must be an integer")


def _validate_oauth_config(raw: object) -> OAuthConfig:
    if not isinstance(raw, dict):
        raise ValueError("oauth must be an object")
    broker_start_url = _validate_draft_string(
        raw.get("broker_start_url"), field_name="oauth.broker_start_url"
    )
    broker_redeem_url = _validate_draft_string(
        raw.get("broker_redeem_url"), field_name="oauth.broker_redeem_url"
    )
    broker_provider = _validate_draft_string(
        raw.get("broker_provider"), field_name="oauth.broker_provider"
    )
    test_url = _validate_draft_string(raw.get("test_url"), field_name="oauth.test_url")
    if broker_provider in BROKER_DEFAULT_TEST_URLS:
        if not broker_start_url:
            broker_start_url = DEFAULT_BROKER_START_URL
        if not broker_redeem_url:
            broker_redeem_url = DEFAULT_BROKER_REDEEM_URL
        if not test_url:
            test_url = BROKER_DEFAULT_TEST_URLS[broker_provider]
    return {
        "client_id": _validate_draft_string(
            raw.get("client_id"), field_name="oauth.client_id"
        ),
        "client_secret": _validate_draft_string(
            raw.get("client_secret"), field_name="oauth.client_secret"
        ),
        "authorization_url": _validate_draft_string(
            raw.get("authorization_url"), field_name="oauth.authorization_url"
        ),
        "token_url": _validate_draft_string(
            raw.get("token_url"), field_name="oauth.token_url"
        ),
        "scopes": _normalize_scopes(raw.get("scopes")),
        "redirect_uri": _validate_draft_string(
            raw.get("redirect_uri"), field_name="oauth.redirect_uri"
        ),
        "test_url": test_url,
        "broker_start_url": broker_start_url,
        "broker_redeem_url": broker_redeem_url,
        "broker_provider": broker_provider,
    }


def _validate_oauth_state(raw: object) -> OAuthState:
    if not isinstance(raw, dict):
        raise ValueError("oauth_state must be an object")
    state: OAuthState = {}
    access_token = _validate_optional_string(
        raw.get("access_token"), field_name="oauth_state.access_token"
    )
    refresh_token = _validate_optional_string(
        raw.get("refresh_token"), field_name="oauth_state.refresh_token"
    )
    token_type = _validate_optional_string(
        raw.get("token_type"), field_name="oauth_state.token_type"
    )
    scope = _validate_optional_string(raw.get("scope"), field_name="oauth_state.scope")
    expires_at = _validate_optional_int(
        raw.get("expires_at"), field_name="oauth_state.expires_at"
    )
    last_checked_at = _validate_optional_int(
        raw.get("last_checked_at"), field_name="oauth_state.last_checked_at"
    )
    status = raw.get("status")
    last_error = _validate_optional_string(
        raw.get("last_error"), field_name="oauth_state.last_error"
    )

    if access_token is not None:
        state["access_token"] = access_token
    if refresh_token is not None:
        state["refresh_token"] = refresh_token
    if token_type is not None:
        state["token_type"] = token_type
    if scope is not None:
        state["scope"] = scope
    if expires_at is not None:
        state["expires_at"] = expires_at
    if last_checked_at is not None:
        state["last_checked_at"] = last_checked_at
    if status is not None:
        if status not in ("valid", "invalid"):
            raise ValueError("oauth_state.status must be valid/invalid")
        state["status"] = cast(OAuthStatus, status)
    if last_error is not None:
        state["last_error"] = last_error
    return state


def validate_profile(raw: object) -> Profile:
    if isinstance(raw, list):
        return profile_from_legacy_entries(raw)
    if not isinstance(raw, dict):
        raise ValueError("profile must be an object")

    raw_type = raw.get("type", "basic")
    if raw_type not in ("basic", "oauth2"):
        raise ValueError("profile.type must be basic/oauth2")
    profile_type = cast(ProfileType, raw_type)

    fields_raw = raw.get("fields", [])
    if not isinstance(fields_raw, list):
        raise ValueError("profile.fields must be a list")
    fields = [_validate_field(item, index) for index, item in enumerate(fields_raw)]
    seen: set[str] = set()
    for field in fields:
        if field["key"] in seen:
            raise ValueError("field keys must be unique")
        seen.add(field["key"])

    profile: Profile = {"type": profile_type, "fields": fields}

    if profile_type == "oauth2":
        profile["oauth"] = _validate_oauth_config(raw.get("oauth"))
        if raw.get("oauth_state") is not None:
            profile["oauth_state"] = _validate_oauth_state(raw.get("oauth_state"))
    elif raw.get("oauth") is not None or raw.get("oauth_state") is not None:
        raise ValueError("basic profiles cannot include oauth data")

    return profile


def template_profile() -> Profile:
    return {
        "type": "basic",
        "fields": [{"key": "secret", "visibility": "private", "value": ""}],
    }


def oauth_template_profile() -> Profile:
    return {
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


def profile_from_legacy_entries(raw: object) -> Profile:
    if not isinstance(raw, list):
        raise ValueError("legacy profile must be a list")
    fields: list[Field] = []
    for index, item in enumerate(raw):
        fields.append(_validate_field(item, index))
    return {"type": "basic", "fields": fields}


def redact_value(value: object) -> object:
    if isinstance(value, dict):
        return {key: redact_value(sub_value) for key, sub_value in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    return REDACTED


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in SENSITIVE_EXACT_KEYS:
        return True
    return any(lowered.endswith(suffix) for suffix in SENSITIVE_SUFFIXES)


def _redact_nested_object(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: redact_value(sub_value)
            if _is_sensitive_key(key)
            else _redact_nested_object(sub_value)
            for key, sub_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_nested_object(item) for item in value]
    return value


def redact_profile(profile: Profile) -> Profile:
    fields: list[Field] = []
    for field in profile["fields"]:
        if field["visibility"] == "private":
            fields.append(
                {
                    "key": field["key"],
                    "visibility": field["visibility"],
                    "value": redact_value(field["value"]),
                }
            )
        else:
            fields.append(field)

    redacted: Profile = {"type": profile["type"], "fields": fields}
    if profile["type"] == "oauth2" and "oauth" in profile:
        redacted["oauth"] = cast(OAuthConfig, _redact_nested_object(profile["oauth"]))
        state = profile.get("oauth_state")
        if state is not None:
            redacted["oauth_state"] = cast(OAuthState, _redact_nested_object(state))
    return redacted


def _resolve_selector_parts(value: object, parts: list[str]) -> object:
    current = value
    for part in parts:
        if isinstance(current, dict):
            if part not in current:
                raise ValueError("selector path not found")
            current = current[part]
            continue
        if isinstance(current, list):
            try:
                index = int(part)
            except ValueError as exc:
                raise ValueError("selector path not found") from exc
            try:
                current = current[index]
            except IndexError as exc:
                raise ValueError("selector path not found") from exc
            continue
        raise ValueError("selector path not found")
    return current


def find_profile_value(profile: Profile, selector: str) -> object:
    parts = selector.split(".")
    if len(parts) < 2:
        raise ValueError("selector must include a field or top-level key path")
    if parts[0] in ("public", "private"):
        visibility, field_key = parts[0], parts[1]
        for field in profile["fields"]:
            if field["visibility"] != visibility or field["key"] != field_key:
                continue
            return _resolve_selector_parts(field["value"], parts[2:])
        raise ValueError("selector path not found")
    return _resolve_selector_parts(profile, parts)


def _collect_string_leaves(value: object) -> list[str]:
    if isinstance(value, dict):
        items: list[str] = []
        for sub_value in value.values():
            items.extend(_collect_string_leaves(sub_value))
        return items
    if isinstance(value, list):
        items: list[str] = []
        for item in value:
            items.extend(_collect_string_leaves(item))
        return items
    if value is None:
        return []
    return [str(value)]


def _serialize_env_value(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def redact_text(text: str, secrets: list[str]) -> str:
    redacted = text
    for secret in sorted({item for item in secrets if item}, key=len, reverse=True):
        redacted = redacted.replace(secret, REDACTED)
    return redacted


def selector_contains_secret(selector: str) -> bool:
    parts = [part for part in selector.split(".") if part]
    if not parts:
        return False
    if parts[0] == "private":
        return True
    return any(_is_sensitive_key(part) for part in parts)


def _response_json(response: requests.Response) -> dict[str, object]:
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise ValueError("oauth response was not valid json") from exc
    if not isinstance(payload, dict):
        raise ValueError("oauth response must be an object")
    return cast(dict[str, object], payload)


def _raise_for_http_error(response: requests.Response, *, action: str) -> None:
    if response.status_code < 400:
        return
    snippet = response.text.strip()
    if len(snippet) > 240:
        snippet = f"{snippet[:237]}..."
    detail = f"{action} failed with HTTP {response.status_code}"
    if snippet:
        detail = f"{detail}: {snippet}"
    raise ValueError(detail)


def _require_oauth_profile(profile: Profile) -> tuple[OAuthConfig, OAuthState]:
    oauth = profile.get("oauth")
    if profile["type"] != "oauth2" or oauth is None:
        raise ValueError("profile is not an oauth2 credential")
    state = cast(OAuthState, dict(profile.get("oauth_state") or {}))
    return oauth, state


def _is_brokered_oauth(oauth: OAuthConfig) -> bool:
    return bool(
        oauth["broker_start_url"].strip() and oauth["broker_redeem_url"].strip()
    )


def _ensure_oauth_ready(oauth: OAuthConfig) -> None:
    missing: list[str] = []
    for key in (
        "client_id",
        "client_secret",
        "authorization_url",
        "token_url",
        "redirect_uri",
        "test_url",
    ):
        value = oauth[key].strip()
        if not value:
            missing.append(key)
    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"oauth profile is incomplete: missing {joined}")


def _is_access_token_fresh(state: OAuthState, *, now: int | None = None) -> bool:
    access_token = state.get("access_token", "").strip()
    if not access_token:
        return False
    expires_at = state.get("expires_at")
    if expires_at is None:
        return True
    current = _now_epoch() if now is None else now
    return expires_at > current + ACCESS_TOKEN_REFRESH_SKEW_SECONDS


def _token_scope_value(
    token_payload: dict[str, object], fallback_state: OAuthState, oauth: OAuthConfig
) -> str | None:
    scope_raw = token_payload.get("scope")
    if isinstance(scope_raw, str) and scope_raw.strip():
        return scope_raw.strip()
    if "scope" in fallback_state and fallback_state["scope"].strip():
        return fallback_state["scope"].strip()
    if oauth["scopes"]:
        return " ".join(oauth["scopes"])
    return None


def _normalize_token_type(token_type: str | None, default: str = "Bearer") -> str:
    value = (token_type or "").strip()
    if not value:
        return default
    if value.lower() == "bearer":
        return "Bearer"
    return value


def _build_oauth_state(
    token_payload: dict[str, object],
    *,
    existing_state: OAuthState,
    oauth: OAuthConfig,
    status: OAuthStatus,
    last_error: str = "",
    checked_now: bool = False,
) -> OAuthState:
    access_token = _validate_required_string(
        token_payload.get("access_token"), field_name="access_token"
    )
    refresh_token = _validate_optional_string(
        token_payload.get("refresh_token"), field_name="refresh_token"
    )
    token_type = _normalize_token_type(
        _validate_optional_string(
            token_payload.get("token_type"), field_name="token_type"
        )
        or existing_state.get("token_type", "Bearer")
    )
    expires_in = _validate_optional_int(
        token_payload.get("expires_in"), field_name="expires_in"
    )
    expires_at = _validate_optional_int(
        token_payload.get("expires_at"), field_name="expires_at"
    )
    scope = _token_scope_value(token_payload, existing_state, oauth)

    state: OAuthState = {
        "access_token": access_token,
        "token_type": token_type,
        "status": status,
        "last_error": last_error,
    }
    if refresh_token is not None:
        state["refresh_token"] = refresh_token
    elif "refresh_token" in existing_state:
        state["refresh_token"] = existing_state["refresh_token"]
    if scope is not None:
        state["scope"] = scope
    if expires_at is not None:
        state["expires_at"] = expires_at
    elif expires_in is not None:
        state["expires_at"] = _now_epoch() + expires_in
    elif "expires_at" in existing_state:
        state["expires_at"] = existing_state["expires_at"]
    if checked_now:
        state["last_checked_at"] = _now_epoch()
    elif "last_checked_at" in existing_state:
        state["last_checked_at"] = existing_state["last_checked_at"]
    return state


def _mark_oauth_invalid(
    profile: Profile, *, message: str, checked_now: bool
) -> Profile:
    oauth, state = _require_oauth_profile(profile)
    next_state = cast(OAuthState, dict(state))
    next_state["status"] = "invalid"
    next_state["last_error"] = message
    if checked_now:
        next_state["last_checked_at"] = _now_epoch()
    next_profile = dict(profile)
    next_profile["oauth"] = oauth
    next_profile["oauth_state"] = next_state
    return validate_profile(next_profile)


def _exchange_authorization_code(
    profile: Profile, *, code: str, redirect_uri: str, code_verifier: str
) -> Profile:
    oauth, state = _require_oauth_profile(profile)
    _ensure_oauth_ready(oauth)
    response = requests.post(
        oauth["token_url"],
        data={
            "grant_type": "authorization_code",
            "client_id": oauth["client_id"],
            "client_secret": oauth["client_secret"],
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    _raise_for_http_error(response, action="oauth authorization")
    token_payload = _response_json(response)

    next_profile = dict(profile)
    next_profile["oauth"] = oauth
    next_profile["oauth_state"] = _build_oauth_state(
        token_payload,
        existing_state=state,
        oauth=oauth,
        status="valid",
        last_error="",
        checked_now=True,
    )
    return validate_profile(next_profile)


def _refresh_brokered_access_token(profile: Profile) -> Profile:
    oauth, state = _require_oauth_profile(profile)
    refresh_token = state.get("refresh_token", "").strip()
    if not refresh_token:
        raise ValueError("oauth token is expired and cannot be refreshed")

    broker_provider = oauth["broker_provider"].strip()
    if not broker_provider:
        raise ValueError("brokered oauth token refresh requires broker_provider")

    broker_redeem_url = oauth["broker_redeem_url"].strip()
    if not broker_redeem_url:
        raise ValueError("brokered oauth token refresh is not configured")

    response = requests.post(
        broker_redeem_url,
        json={
            "provider": broker_provider,
            "refresh_token": refresh_token,
        },
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    _raise_for_http_error(response, action="oauth broker refresh")
    payload = _response_json(response)
    token_payload = payload.get("token_payload")
    if not isinstance(token_payload, dict):
        error_code = payload.get("error")
        error_description = payload.get("error_description")
        detail_parts: list[str] = []
        if isinstance(error_code, str) and error_code.strip():
            detail_parts.append(error_code.strip())
        if isinstance(error_description, str) and error_description.strip():
            detail_parts.append(error_description.strip())
        if detail_parts:
            raise ValueError(f"oauth broker refresh failed: {': '.join(detail_parts)}")
        raise ValueError("oauth broker refresh response must include token_payload")

    next_profile = dict(profile)
    next_profile["oauth"] = oauth
    next_profile["oauth_state"] = _build_oauth_state(
        cast(dict[str, object], token_payload),
        existing_state=state,
        oauth=oauth,
        status="valid",
        last_error="",
        checked_now=False,
    )
    return validate_profile(next_profile)


def _refresh_access_token(profile: Profile) -> Profile:
    oauth, state = _require_oauth_profile(profile)
    if _is_brokered_oauth(oauth):
        return _refresh_brokered_access_token(profile)
    _ensure_oauth_ready(oauth)
    refresh_token = state.get("refresh_token", "").strip()
    if not refresh_token:
        raise ValueError("oauth token is expired and cannot be refreshed")

    response = requests.post(
        oauth["token_url"],
        data={
            "grant_type": "refresh_token",
            "client_id": oauth["client_id"],
            "client_secret": oauth["client_secret"],
            "refresh_token": refresh_token,
        },
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    _raise_for_http_error(response, action="oauth token refresh")
    token_payload = _response_json(response)

    next_profile = dict(profile)
    next_profile["oauth"] = oauth
    next_profile["oauth_state"] = _build_oauth_state(
        token_payload,
        existing_state=state,
        oauth=oauth,
        status="valid",
        last_error="",
        checked_now=False,
    )
    return validate_profile(next_profile)


def ensure_fresh_access_token(
    store: "CredentialStore",
    credential_key: str,
    profile: Profile,
) -> tuple[Profile, str]:
    _oauth, state = _require_oauth_profile(profile)
    if _is_access_token_fresh(state):
        access_token = state.get("access_token", "").strip()
        if not access_token:
            raise ValueError("oauth token is missing")
        return profile, access_token

    with _credential_refresh_lock(credential_key):
        current_profile = store.get_profile(credential_key)
        if current_profile is not None:
            profile = current_profile
        _oauth, current_state = _require_oauth_profile(profile)
        if _is_access_token_fresh(current_state):
            access_token = current_state.get("access_token", "").strip()
            if not access_token:
                raise ValueError("oauth token is missing")
            return profile, access_token

        try:
            refreshed = _refresh_access_token(profile)
        except (requests.RequestException, ValueError) as exc:
            invalid = _mark_oauth_invalid(profile, message=str(exc), checked_now=False)
            store.set_profile(credential_key, invalid)
            raise ValueError(str(exc)) from exc

        store.set_profile(credential_key, refreshed)
        refreshed_state = refreshed.get("oauth_state") or {}
        access_token = refreshed_state.get("access_token", "").strip()
        if not access_token:
            raise ValueError("oauth refresh did not return an access token")
        return refreshed, access_token


def _selector_requires_fresh_access_token(profile: Profile, selector: str) -> bool:
    if profile["type"] != "oauth2":
        return False
    normalized = selector.strip()
    if not normalized:
        return False
    return normalized == "oauth_state.access_token"


def test_oauth_token(
    store: "CredentialStore", credential_key: str, profile: Profile
) -> tuple[Profile, dict[str, object]]:
    oauth, state = _require_oauth_profile(profile)
    profile_with_token, access_token = ensure_fresh_access_token(
        store, credential_key, profile
    )
    _, state = _require_oauth_profile(profile_with_token)
    token_type = _normalize_token_type(state.get("token_type"), "Bearer")

    try:
        response = requests.get(
            oauth["test_url"],
            headers={"Authorization": f"{token_type} {access_token}"},
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        invalid = _mark_oauth_invalid(
            profile_with_token, message=str(exc), checked_now=True
        )
        store.set_profile(credential_key, invalid)
        raise ValueError(str(exc)) from exc

    if 200 <= response.status_code < 300:
        next_profile = dict(profile_with_token)
        current_state = cast(OAuthState, profile_with_token.get("oauth_state") or {})
        next_state = cast(OAuthState, dict(current_state))
        next_state["status"] = "valid"
        next_state["last_error"] = ""
        next_state["last_checked_at"] = _now_epoch()
        next_profile["oauth_state"] = next_state
        validated = validate_profile(next_profile)
        store.set_profile(credential_key, validated)
        return validated, {
            "status": "valid",
            "checked_at": next_state["last_checked_at"],
            "message": "OAuth token is valid.",
        }

    snippet = response.text.strip()
    if len(snippet) > 240:
        snippet = f"{snippet[:237]}..."
    message = f"health check failed with HTTP {response.status_code}"
    if snippet:
        message = f"{message}: {snippet}"
    invalid = _mark_oauth_invalid(profile_with_token, message=message, checked_now=True)
    store.set_profile(credential_key, invalid)
    raise ValueError(message)


class CredentialStore:
    def __init__(
        self, backend: StoreBackend | None = None, service_name: str = SERVICE_NAME
    ):
        self.backend = backend or KeyringBackend()
        self.service_name = service_name

    def set_profile(self, credential_key: str, profile: Profile) -> None:
        serialized = json.dumps(
            validate_profile(profile), ensure_ascii=False, sort_keys=True
        )
        self.backend.set_password(
            self.service_name, validate_credential_key(credential_key), serialized
        )
        keys = self._load_index()
        keys.add(credential_key)
        self._save_index(keys)

    def get_profile(self, credential_key: str) -> Profile | None:
        serialized = self.backend.get_password(
            self.service_name, validate_credential_key(credential_key)
        )
        if serialized is None:
            return None
        return validate_profile(json.loads(serialized))

    def delete_profile(self, credential_key: str) -> bool:
        key = validate_credential_key(credential_key)
        existing = self.backend.get_password(self.service_name, key)
        if existing is None:
            return False
        self.backend.delete_password(self.service_name, key)
        keys = self._load_index()
        if key in keys:
            keys.remove(key)
            self._save_index(keys)
        return True

    def list_keys(self) -> list[str]:
        keys = self._load_index()
        keys.update(self._list_keys_from_backend())
        return sorted(keys)

    def _load_index(self) -> set[str]:
        serialized = self.backend.get_password(self.service_name, INDEX_USERNAME)
        if serialized is None:
            return set()
        try:
            parsed = json.loads(serialized)
        except json.JSONDecodeError:
            return set()
        if not isinstance(parsed, list):
            return set()
        return {value for value in parsed if isinstance(value, str)}

    def _save_index(self, keys: set[str]) -> None:
        self.backend.set_password(
            self.service_name, INDEX_USERNAME, json.dumps(sorted(keys))
        )

    def _list_keys_from_backend(self) -> set[str]:
        list_usernames = getattr(self.backend, "list_usernames", None)
        if list_usernames is None:
            return set()
        try:
            usernames = list_usernames(self.service_name)
        except Exception:
            return set()
        keys: set[str] = set()
        for username in usernames:
            if not isinstance(username, str) or username == INDEX_USERNAME:
                continue
            try:
                keys.add(validate_credential_key(username))
            except ValueError:
                continue
        return keys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-secrets")
    subparsers = parser.add_subparsers(dest="command", required=True)

    template_parser = subparsers.add_parser("template")
    template_parser.add_argument("--file", dest="file_path", default=None)
    template_parser.add_argument(
        "--oauth", action="store_true", help="Write an oauth2 template profile."
    )

    get_parser = subparsers.add_parser("get")
    get_parser.add_argument("credential_key")

    raw_get_parser = subparsers.add_parser("app-get")
    raw_get_parser.add_argument("credential_key")

    set_parser = subparsers.add_parser("set")
    set_parser.add_argument("credential_key")
    set_group = set_parser.add_mutually_exclusive_group(required=True)
    set_group.add_argument("--file", dest="file_path")
    set_group.add_argument("--stdin-json", action="store_true")

    delete_parser = subparsers.add_parser("delete")
    delete_parser.add_argument("credential_key")

    subparsers.add_parser("list")

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--profile", dest="credential_key", required=True)
    run_parser.add_argument(
        "--env",
        dest="env_mappings",
        action="append",
        default=[],
        help="Mapping VAR=selector, for example GITHUB_TOKEN=private.token or SLACK_TOKEN=oauth_state.access_token",
    )
    run_parser.add_argument("cmd", nargs=argparse.REMAINDER)

    get_access_token_parser = subparsers.add_parser("get-access-token")
    get_access_token_parser.add_argument("credential_key")

    oauth_authorize_parser = subparsers.add_parser("oauth-authorize")
    oauth_authorize_parser.add_argument("credential_key")
    oauth_authorize_parser.add_argument("--code", required=True)
    oauth_authorize_parser.add_argument("--redirect-uri", required=True)
    oauth_authorize_parser.add_argument("--code-verifier", required=True)

    oauth_test_parser = subparsers.add_parser("oauth-test")
    oauth_test_parser.add_argument("credential_key")

    return parser


def _error(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def _load_profile_from_path(file_path: str) -> Profile:
    payload = json.loads(Path(file_path).read_text(encoding="utf-8"))
    return validate_profile(payload)


def _load_profile_from_stdin() -> Profile:
    payload = json.loads(sys.stdin.read())
    return validate_profile(payload)


def _default_template_path() -> Path:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if runtime_dir:
        return Path(runtime_dir) / "agent_secrets_template.json"
    return Path("/tmp") / f"agent_secrets_{os.getuid()}_template.json"


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def run_cli(argv: list[str] | None = None, backend: StoreBackend | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = CredentialStore(backend=backend)

    if args.command == "template":
        path = Path(args.file_path) if args.file_path else _default_template_path()
        profile = oauth_template_profile() if args.oauth else template_profile()
        _write_text(path, json.dumps(profile, indent=2, sort_keys=True) + "\n")
        print(str(path))
        return 0

    if args.command == "list":
        for credential_key in store.list_keys():
            print(credential_key)
        return 0

    if args.command == "get":
        try:
            profile = store.get_profile(args.credential_key)
        except ValueError:
            return _error("invalid credential key")
        if profile is None:
            return _error("credential not found")
        print(json.dumps(redact_profile(profile), indent=2, sort_keys=True))
        return 0

    if args.command == "app-get":
        if os.environ.get("AGENT_SECRETS_ALLOW_RAW_STDOUT") != "1":
            return _error("raw profile output is disabled")
        try:
            profile = store.get_profile(args.credential_key)
        except ValueError:
            return _error("invalid credential key")
        if profile is None:
            return _error("credential not found")
        print(json.dumps(profile, indent=2, sort_keys=True))
        return 0

    if args.command == "set":
        try:
            validate_credential_key(args.credential_key)
            profile = (
                _load_profile_from_path(args.file_path)
                if args.file_path
                else _load_profile_from_stdin()
            )
            store.set_profile(args.credential_key, profile)
        except (OSError, ValueError, json.JSONDecodeError):
            return _error("invalid credential key or profile input")
        print("ok")
        return 0

    if args.command == "delete":
        try:
            deleted = store.delete_profile(args.credential_key)
        except ValueError:
            return _error("invalid credential key")
        if not deleted:
            return _error("credential not found")
        print("deleted")
        return 0

    if args.command == "get-access-token":
        try:
            profile = store.get_profile(args.credential_key)
        except ValueError:
            return _error("invalid credential key")
        if profile is None:
            return _error("credential not found")
        try:
            _profile, access_token = ensure_fresh_access_token(
                store, args.credential_key, profile
            )
        except ValueError as exc:
            return _error(str(exc))
        print(access_token)
        return 0

    if args.command == "oauth-authorize":
        try:
            profile = store.get_profile(args.credential_key)
        except ValueError:
            return _error("invalid credential key")
        if profile is None:
            return _error("credential not found")
        try:
            next_profile = _exchange_authorization_code(
                profile,
                code=args.code,
                redirect_uri=args.redirect_uri,
                code_verifier=args.code_verifier,
            )
            store.set_profile(args.credential_key, next_profile)
        except (requests.RequestException, ValueError) as exc:
            invalid = _mark_oauth_invalid(profile, message=str(exc), checked_now=True)
            store.set_profile(args.credential_key, invalid)
            return _error(str(exc))
        print(json.dumps({"status": "valid"}, sort_keys=True))
        return 0

    if args.command == "oauth-test":
        try:
            profile = store.get_profile(args.credential_key)
        except ValueError:
            return _error("invalid credential key")
        if profile is None:
            return _error("credential not found")
        try:
            _profile, payload = test_oauth_token(store, args.credential_key, profile)
        except ValueError as exc:
            return _error(str(exc))
        print(json.dumps(payload, sort_keys=True))
        return 0

    if args.command == "run":
        try:
            profile = store.get_profile(args.credential_key)
        except ValueError:
            return _error("invalid credential key")
        if profile is None:
            return _error("credential not found")

        if not args.cmd:
            return _error("missing command (use -- <cmd...>)")

        env_updates: dict[str, str] = {}
        private_strings: list[str] = []
        resolved_profile = profile
        for item in args.env_mappings:
            if not isinstance(item, str) or "=" not in item:
                return _error("invalid --env (expected VAR=selector)")
            env_var, selector = item.split("=", 1)
            if not env_var or not selector:
                return _error("invalid --env (expected VAR=selector)")
            try:
                if _selector_requires_fresh_access_token(resolved_profile, selector):
                    resolved_profile, _access_token = ensure_fresh_access_token(
                        store, args.credential_key, resolved_profile
                    )
                value = find_profile_value(resolved_profile, selector)
            except ValueError as exc:
                if str(exc) == "selector path not found":
                    return _error("invalid mapping selector")
                return _error(str(exc))
            serialized = _serialize_env_value(value)
            env_updates[env_var] = serialized
            if selector_contains_secret(selector):
                private_strings.extend(_collect_string_leaves(value))
                private_strings.append(serialized)

        cmd = list(args.cmd)
        if cmd and cmd[0] == "--":
            cmd = cmd[1:]
        if not cmd:
            return _error("missing command (use -- <cmd...>)")

        env = dict(os.environ)
        env.update(env_updates)
        result = subprocess.run(cmd, capture_output=True, env=env, check=False)
        stdout = redact_text(
            result.stdout.decode("utf-8", errors="replace"), private_strings
        )
        stderr = redact_text(
            result.stderr.decode("utf-8", errors="replace"), private_strings
        )
        if stdout:
            sys.stdout.write(stdout)
        if stderr:
            sys.stderr.write(stderr)
        return int(result.returncode)

    return _error("unknown command")


def main() -> int:
    return run_cli()


if __name__ == "__main__":
    raise SystemExit(main())
