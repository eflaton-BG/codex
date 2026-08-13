---
name: bga-connections
description: Use BGA Platform connections from local Codex through BG AI Gateway without exposing provider credentials.
---

# BGA Connections

Use the bundled `bga-connections.py` script to list BGA connections, inspect permissions, call approved provider endpoints, download large responses, upload multipart content, query governed operational datasets, perform explicitly authorized structured dataset administration, request unsupported endpoints, send approved Slack messages, and post bounded GitHub `COMMENT` reviews.

The script requires installer-managed `BG_AI_GATEWAY_API_KEY` and `BG_AI_GATEWAY_BASE_URL` values. It sends provider requests through BG AI Gateway so OAuth tokens and provider credentials stay server-side.

Before calling a connection, inspect its permissions. Use `download` for files or when a `call` response reports `bodyTruncated: true`. Use `--body-file`, `--body-base64-file`, or `--multipart-json` instead of embedding large content in a shell command.

For Slack attachments, first search messages or read the thread, extract the Slack file ID, inspect it with `call <connection-id> --method POST --path /files.info --body-text file=<file-id>`, then use `slack-file-download <connection-id> <file-id> --output <workspace-relative-path>`. The download command resolves the private URL server-side and never accepts a copied Slack private URL. It requires the Slack `File details` and `Download file` read capabilities plus the connected user's current Slack access. If unavailable, report the returned capability, scope, visibility, size, timeout, or redirect error rather than inventing or reusing unrelated media.

All `bga-connections` Slack calls, including the existing `files.getUploadURLExternal` and `files.completeUploadExternal` upload sequence, act as the saved connected Slack user. They are separate from `POST /platform/slack/files`, which is available only to a running Agent Gateway and uploads one runtime-workspace file as the BG Agents Slack app. This local client intentionally does not expose that bot-identity upload path.

For common multipart uploads, pass text fields with `--multipart-text name=value` and files with `--multipart-file name=path`.

Use `send-slack-message --channel <channel-id> --section <text>` only when the user explicitly asks to send that message. Use `post-github-review --owner <owner> --repo <repo> --pull-number <number> --head-sha <sha> --body <review> --explicit-user-request` only after an explicit user request. The platform derives the principal, keeps credentials server-side, verifies repository access and the current pull-request head, and always posts a `COMMENT` review.

Use `datasets-list` and `datasets-query --sql-file <path>` for governed data. Dataset queries run as the local Codex key's current principal, never as an agent. They accept one `SELECT`, `INSERT`, `UPDATE`, or `DELETE` with ordinary table expressions; explicit functions are limited to `count`, `sum`, `avg`, `min`, and `max`. Structured administration commands are `datasets-admin-list`, `datasets-admin-create`, `datasets-admin-update`, `datasets-admin-delete`, `datasets-admin-add-column`, `datasets-admin-rename-column`, `datasets-admin-create-index`, `datasets-admin-delete-index`, `datasets-admin-list-grants`, and `datasets-admin-set-grant`. Mutation payloads use `--json-file`; the platform requires the current principal's narrow dataset administration permission and audits the actual principal.
