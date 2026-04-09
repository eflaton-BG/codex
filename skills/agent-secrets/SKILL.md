---
name: agent-secrets
description: "Use Ubuntu keyring-backed field and OAuth credential profiles without exposing secrets in chat; prefer redacted inspection, env injection, and in-process API calls."
---

# agent-secrets

Use this skill when credentials should stay in Ubuntu keyring and out of Codex chat/session history.

Primary script:
- `python codex_skills_library/agent-secrets/scripts/agent_secrets.py --help`

Profile types you may encounter:
- Field-based profiles: normal `{"fields": [...]}` entries meant for `get` and `run --env`.
- OAuth-backed profiles: BG also stores some credentials as top-level `oauth2` records with keys like `type`, `oauth`, and `oauth_state`. These can legitimately show `fields: []` even when the credential is healthy.

Rules:
- Never ask the user to paste secrets into chat.
- Never pass private values as plain CLI arguments.
- Prefer `get` for redacted inspection and `run` for env injection on field-based profiles.
- If `get` shows an empty `fields` list, do not assume the credential is missing or broken; it may be OAuth-backed.
- Never print raw keyring payloads or access tokens.
- Treat `app-get` as last resort only. It is gated for a reason and should never dump raw secret material into chat.

## Fast path
List available profiles:
```bash
python codex_skills_library/agent-secrets/scripts/agent_secrets.py list
```

Inspect a profile without revealing private values:
```bash
python codex_skills_library/agent-secrets/scripts/agent_secrets.py get github/default
```

If `get` returns `{"fields": []}`, switch to the OAuth-backed flow below instead of retrying field selectors.

## Field-based flow
Create a template profile file:
```bash
python codex_skills_library/agent-secrets/scripts/agent_secrets.py template \
  --file /tmp/github-default.json
```

Store a profile from file:
```bash
python codex_skills_library/agent-secrets/scripts/agent_secrets.py set github/default \
  --file /tmp/github-default.json
```

Run a command with injected secrets:
```bash
python codex_skills_library/agent-secrets/scripts/agent_secrets.py run \
  --profile github/default \
  --env GITHUB_TOKEN=private.token \
  -- python -c "import os; print(bool(os.environ['GITHUB_TOKEN']))"
```

## Profile shape
Profiles are JSON objects:

```json
{
  "fields": [
    { "key": "base_url", "visibility": "public", "value": "https://api.github.com" },
    { "key": "token", "visibility": "private", "value": "ghp_example" }
  ]
}
```

Selector format for `run --env`:
- `private.token`
- `public.base_url`
- nested values are allowed, for example `private.oauth.client_secret`

`get` always redacts private values as `"***"`.

## OAuth-backed flow
Current BG OAuth-backed entries may include top-level keys like:

```json
{
  "type": "oauth2",
  "fields": [],
  "oauth": { "broker_provider": "slack", "client_secret": "..." },
  "oauth_state": {
    "status": "valid",
    "scope": "channels:history,...",
    "token_type": "user",
    "access_token": "xoxp-..."
  }
}
```

`get` now preserves non-secret OAuth metadata and redacts secret fields. `run --env` also supports top-level selectors such as `oauth_state.access_token`. When you hit an OAuth-backed profile:

1. Confirm the profile exists with `list`.
2. Use `get` once; if `fields` is empty, treat it as OAuth-backed and inspect the redacted metadata.
3. Use `run --env` with a root selector for the token you need.
4. Keep command output non-secret; the runner will redact sensitive child output, but do not intentionally print tokens.

Redacted inspection example:
```bash
python codex_skills_library/agent-secrets/scripts/agent_secrets.py get slack/atl
```

Safe API usage example:
```bash
python codex_skills_library/agent-secrets/scripts/agent_secrets.py run \
  --profile slack/atl \
  --env SLACK_TOKEN=oauth_state.access_token \
  -- python -c "import os, requests; r = requests.post('https://slack.com/api/auth.test', headers={'Authorization': f\"Bearer {os.environ['SLACK_TOKEN']}\"}, timeout=30); d = r.json(); print({'ok': d.get('ok'), 'team': d.get('team'), 'user': d.get('user')})"
```

For Slack permalinks, convert `.../p1773263989479869` to thread ts `1773263989.479869` before calling `conversations.replies`.
