---
name: private-slack-status-updates
description: Send concise private Slack updates, especially Pittston downloader status updates, into the private channel `#codex_and_zeke` with minimal prompt tokens. Use when Codex needs to post a short private update or recurring progress heartbeat to Slack and wants bundled scripts to prepare the exact `channel_id` and `message` payload, generate those payloads on a fixed cadence, or send them directly to Slack from a local standalone process.
---

# Private Slack Status Updates

Use this skill when a private Slack update should be sent to `#codex_and_zeke`, especially when the user asks for consistent recurring updates.

## Channel

- Private channel name: `#codex_and_zeke`
- Channel id: `C0ATGDN0B9Q`

## Default Pattern

1. Generate the Slack payload locally with `scripts/prepare_slack_update.py`.
2. If the user wants consistent updates, start `scripts/run_prepared_slack_update_loop.py` in the background.
3. Default the cadence to every 5 minutes unless the user asks for a different interval.
4. Use the newest generated JSON payload with the Slack send tool.
5. Keep messages short and factual.

## Standalone Pattern

If the user wants the machine to post updates without an active Codex session:

1. Put a bot token in an env file, for example from `templates/slack-bot.env.example`.
2. Start `scripts/run_generate_and_send_slack_update_loop.py`.
3. Default the cadence to every 5 minutes unless the user asks for a different interval.
4. Run it under `systemd-run --user` so it survives the shell.

## Important Limitation

- The original background loop only prepares payloads locally.
- The standalone sender path needs a valid Slack bot token with `chat:write` permission and channel access.
- Do not pass the token as a CLI argument. Use an env var or env file.

## Bundled Scripts

### `prepare_slack_update.py`

Outputs a single JSON object with:

- `channel_id`
- `message`

#### Raw message mode

```bash
/usr/bin/python3 /home/ezekiel.flaton@berkshiregrey.com/.codex/skills/private-slack-status-updates/scripts/prepare_slack_update.py   --message "Short private update"
```

#### Pittston copy-state mode

```bash
/usr/bin/python3 /home/ezekiel.flaton@berkshiregrey.com/.codex/skills/private-slack-status-updates/scripts/prepare_slack_update.py   --copy-state-dir /home/ezekiel.flaton@berkshiregrey.com/Downloads/pittston_revs_images_2026-04-09/.copy-state   --label "Pittston downloader"   --pid 2219879   --next-update-minutes 5
```

### `run_prepared_slack_update_loop.py`

Runs `prepare_slack_update.py` on a cadence and writes the generated payloads to a local state directory.

It writes:

- `latest_payload.json`
- `history.jsonl`
- `latest_sequence.txt`

Example:

```bash
/usr/bin/systemd-run --user --unit codex-private-slack-status-pittston --collect   /bin/bash -lc 'exec /usr/bin/python3 -u /home/ezekiel.flaton@berkshiregrey.com/.codex/skills/private-slack-status-updates/scripts/run_prepared_slack_update_loop.py     --state-dir /tmp/private-slack-status-updates/pittston-image-download     --interval-seconds 300     --copy-state-dir /home/ezekiel.flaton@berkshiregrey.com/Downloads/pittston_revs_images_2026-04-09/.copy-state     --label "Pittston downloader"     --pid 2219879     --next-update-minutes 5     >>/tmp/private-slack-status-updates/pittston-image-download/live.log 2>&1'
```

For one validation cycle before backgrounding it:

```bash
/usr/bin/python3 /home/ezekiel.flaton@berkshiregrey.com/.codex/skills/private-slack-status-updates/scripts/run_prepared_slack_update_loop.py   --state-dir /tmp/private-slack-status-updates/pittston-image-download   --interval-seconds 300   --max-runs 1   --copy-state-dir /home/ezekiel.flaton@berkshiregrey.com/Downloads/pittston_revs_images_2026-04-09/.copy-state   --label "Pittston downloader"   --pid 2219879   --next-update-minutes 5
```

### `send_prepared_slack_update.py`

Reads generated payloads from a state directory, posts any unsent sequences to Slack with `chat.postMessage`, and tracks progress locally.

It writes:

- `last_sent_sequence.txt`
- `send_results.jsonl`

Dry-run example:

```bash
/usr/bin/python3 /home/ezekiel.flaton@berkshiregrey.com/.codex/skills/private-slack-status-updates/scripts/send_prepared_slack_update.py   --state-dir /tmp/private-slack-status-updates/pittston-image-download   --dry-run
```

Real send example with an env file:

```bash
/usr/bin/python3 /home/ezekiel.flaton@berkshiregrey.com/.codex/skills/private-slack-status-updates/scripts/send_prepared_slack_update.py   --state-dir /tmp/private-slack-status-updates/pittston-image-download   --token-env-file /home/ezekiel.flaton@berkshiregrey.com/.config/codex/slack-bot.env
```

### `run_generate_and_send_slack_update_loop.py`

Runs one payload-generation cycle, sends any new payloads directly to Slack, and repeats on a cadence.

Example:

```bash
/usr/bin/systemd-run --user --unit codex-private-slack-status-pittston-standalone --collect   /bin/bash -lc 'exec /usr/bin/python3 -u /home/ezekiel.flaton@berkshiregrey.com/.codex/skills/private-slack-status-updates/scripts/run_generate_and_send_slack_update_loop.py     --state-dir /tmp/private-slack-status-updates/pittston-image-download     --interval-seconds 300     --token-env-file /home/ezekiel.flaton@berkshiregrey.com/.config/codex/slack-bot.env     --copy-state-dir /home/ezekiel.flaton@berkshiregrey.com/Downloads/pittston_revs_images_2026-04-09/.copy-state     --label "Pittston downloader"     --pid 2219879     --next-update-minutes 5     >>/tmp/private-slack-status-updates/pittston-image-download/standalone-live.log 2>&1'
```

### `templates/slack-bot.env.example`

Template env file for the standalone sender:

```bash
SLACK_BOT_TOKEN=xoxb-your-token-here
```

## Message Shape

- First line: label and current local timestamp
- Second line: copied, failed, pending
- Third line: downloader health when a pid is provided
- Fourth line: latest success timestamp and filename when available
- Final line: next expected update when requested

## Preferred Usage

- When the user says they want consistent updates, assume they want the cadence loop unless they say otherwise.
- Default to a 5-minute interval.
- Start the loop first, preferably with `systemd-run --user`, verify that `latest_payload.json` is being written, and then send the newest payload to Slack.
- Reuse the same `state-dir` for the same monitored workflow so sequence numbers and history stay continuous.
- If the user wants autonomous posting, switch to `run_generate_and_send_slack_update_loop.py` with a token env file instead of relying on Codex to forward messages.
