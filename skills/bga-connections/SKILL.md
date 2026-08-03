---
name: bga-connections
description: Use BGA Platform connections from local Codex through BG AI Gateway without exposing provider credentials.
---

# BGA Connections

Use the bundled `bga-connections.py` script to list BGA connections, inspect permissions, call approved provider endpoints, download large responses, upload multipart content, request unsupported endpoints, send approved Slack messages, and post bounded GitHub `COMMENT` reviews.

The script requires installer-managed `BG_AI_GATEWAY_API_KEY` and `BG_AI_GATEWAY_BASE_URL` values. It sends provider requests through BG AI Gateway so OAuth tokens and provider credentials stay server-side.

Before calling a connection, inspect its permissions. Use `download` for files or when a `call` response reports `bodyTruncated: true`. Use `--body-file`, `--body-base64-file`, or `--multipart-json` instead of embedding large content in a shell command.

For common multipart uploads, pass text fields with `--multipart-text name=value` and files with `--multipart-file name=path`.

Use `send-slack-message --channel <channel-id> --section <text>` only when the user explicitly asks to send that message. Use `post-github-review --owner <owner> --repo <repo> --pull-number <number> --head-sha <sha> --body <review> --explicit-user-request` only after an explicit user request. The platform derives the principal, keeps credentials server-side, verifies repository access and the current pull-request head, and always posts a `COMMENT` review.
