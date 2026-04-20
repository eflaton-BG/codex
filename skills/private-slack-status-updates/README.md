# Private Slack Status Updates

This skill exists to make recurring private Slack updates cheap and repeatable.

It was built for cases where Codex is monitoring a long-running workflow, such as the Pittston image download, and the user wants short status messages sent to a private Slack destination without having to manually compose each message.

## Intended Purpose

Use this skill when you want one of these outcomes:

- send a one-off private Slack status update with minimal prompt tokens
- generate recurring update payloads on a fixed cadence
- post those recurring updates directly to Slack without requiring an active Codex turn

The default target channel for this skill is:

- `#codex_and_zeke`
- channel id: `C0ATGDN0B9Q`

## How It Works

There are two modes.

### 1. Session-driven mode

In this mode, a local loop generates Slack payloads, but Codex still sends the actual Slack message.

This is useful when:

- you want consistent update content
- you do not yet have a bot token
- you still want a human or active Codex session in the loop

### 2. Standalone mode

In this mode, the machine both generates and sends the updates.

This is useful when:

- you want the updates to continue without an active Codex turn
- you have a Slack bot token
- the Slack app has already been invited to the target private channel

## Files and Scripts

### Main instructions

- `SKILL.md`: Codex-facing usage guidance

### Scripts

- `scripts/prepare_slack_update.py`
  - builds a single Slack payload JSON object
- `scripts/run_prepared_slack_update_loop.py`
  - generates payloads on a cadence and stores them locally
- `scripts/send_prepared_slack_update.py`
  - sends prepared payloads to Slack and tracks what has already been sent
- `scripts/run_generate_and_send_slack_update_loop.py`
  - runs generation and sending together on a cadence

### Template

- `templates/slack-bot.env.example`
  - example env file for the Slack bot token

## Setup Requirements

### Minimum requirements

- Python available at `/usr/bin/python3`
- a writable local state directory, typically under `/tmp/private-slack-status-updates/...`

### Additional requirements for standalone posting

- a Slack app with bot posting permission
- bot token scope `chat:write`
- the bot invited to the private target channel
- a token env file containing:

```bash
SLACK_BOT_TOKEN=xoxb-...
```

Recommended token path:

```bash
/home/ezekiel.flaton@berkshiregrey.com/.config/codex/slack-bot.env
```

Do not pass the token on the command line.

## Local State

The skill stores generated payloads and send state in a local state directory, for example:

```bash
/tmp/private-slack-status-updates/pittston-image-download
```

Important files in that directory:

- `latest_payload.json`
- `history.jsonl`
- `latest_sequence.txt`
- `last_sent_sequence.txt`
- `send_results.jsonl`
- `live.log`
- `standalone-live.log`

These files are what let the skill resume cleanly without reposting older messages.

## Common Workflows

### One-off message

Generate a single payload from a raw message:

```bash
/usr/bin/python3 /home/ezekiel.flaton@berkshiregrey.com/.codex/skills/private-slack-status-updates/scripts/prepare_slack_update.py \
  --message "Short private update"
```

### Generate updates every 5 minutes, but do not post automatically

```bash
/usr/bin/systemd-run --user --unit codex-private-slack-status-pittston --collect \
  /bin/bash -lc 'exec /usr/bin/python3 -u /home/ezekiel.flaton@berkshiregrey.com/.codex/skills/private-slack-status-updates/scripts/run_prepared_slack_update_loop.py \
    --state-dir /tmp/private-slack-status-updates/pittston-image-download \
    --interval-seconds 300 \
    --copy-state-dir /home/ezekiel.flaton@berkshiregrey.com/Downloads/pittston_revs_images_2026-04-09/.copy-state \
    --label "Pittston downloader" \
    --next-update-minutes 5 \
    >>/tmp/private-slack-status-updates/pittston-image-download/live.log 2>&1'
```

### Post autonomously every 5 minutes

```bash
/usr/bin/systemd-run --user --unit codex-private-slack-status-pittston-standalone --collect \
  /bin/bash -lc 'exec /usr/bin/python3 -u /home/ezekiel.flaton@berkshiregrey.com/.codex/skills/private-slack-status-updates/scripts/run_generate_and_send_slack_update_loop.py \
    --state-dir /tmp/private-slack-status-updates/pittston-image-download \
    --interval-seconds 300 \
    --token-env-file /home/ezekiel.flaton@berkshiregrey.com/.config/codex/slack-bot.env \
    --copy-state-dir /home/ezekiel.flaton@berkshiregrey.com/Downloads/pittston_revs_images_2026-04-09/.copy-state \
    --label "Pittston downloader" \
    --next-update-minutes 5 \
    >>/tmp/private-slack-status-updates/pittston-image-download/standalone-live.log 2>&1'
```

## Operational Notes

- Default cadence is 5 minutes unless the user asks for something else.
- Reuse the same `state-dir` for the same workflow so sequence tracking stays continuous.
- If the monitored process is not running, the status message will reflect the last saved state rather than live progress.
- The standalone sender is only as fresh as the source state it is reading.

## Verifying It Is Working

Check the service:

```bash
/usr/bin/systemctl --user show codex-private-slack-status-pittston-standalone -p ActiveState -p SubState -p MainPID
```

Check the latest sent records:

```bash
/usr/bin/tail -n 20 /tmp/private-slack-status-updates/pittston-image-download/send_results.jsonl
```

Check the send ledger:

```bash
/usr/bin/sed -n '1,20p' /tmp/private-slack-status-updates/pittston-image-download/last_sent_sequence.txt
```

## Stopping It

Stop the standalone publisher:

```bash
/usr/bin/systemctl --user stop codex-private-slack-status-pittston-standalone.service
```

Stop the payload-only generator:

```bash
/usr/bin/systemctl --user stop codex-private-slack-status-pittston.service
```

## Current Intended Use

The main intended use today is private status reporting for long-running operational tasks, especially:

- Pittston image download progress
- other Codex-monitored jobs where updates should go only to a private Slack destination

The design is intentionally simple:

- small local JSON state
- resumable send ledger
- no token on the command line
- optional full autonomy when a Slack bot token is available
