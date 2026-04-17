#!/usr/bin/env python3
"""Send prepared Slack payloads directly to Slack and track sent sequence numbers."""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path


DEFAULT_TOKEN_ENV_VAR = "SLACK_BOT_TOKEN"
POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send prepared Slack payloads to Slack.")
    parser.add_argument("--state-dir", required=True, help="Directory containing generated payload state.")
    parser.add_argument("--payload-file", help="Optional explicit latest payload file path.")
    parser.add_argument("--history-file", help="Optional explicit payload history file path.")
    parser.add_argument("--last-sent-file", help="Optional explicit send-ledger file path.")
    parser.add_argument("--results-file", help="Optional explicit send results file path.")
    parser.add_argument(
        "--token-env-var",
        default=DEFAULT_TOKEN_ENV_VAR,
        help=f"Environment variable that holds the Slack token. Default: {DEFAULT_TOKEN_ENV_VAR}.",
    )
    parser.add_argument("--token-env-file", help="Optional KEY=VALUE env file that contains the Slack token.")
    parser.add_argument("--latest-only", action="store_true", help="Send only the latest payload if it is newer.")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be sent without calling Slack.")
    return parser.parse_args()


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True))
        handle.write("\n")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_last_sent_sequence(path: Path) -> int:
    if not path.exists():
        return 0
    text = path.read_text(encoding="utf-8").strip()
    return int(text) if text else 0


def write_last_sent_sequence(path: Path, sequence: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{sequence}\n", encoding="utf-8")


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, sep, value = line.partition("=")
        if not sep:
            continue
        values[key.strip()] = value.strip().strip("'").strip('"')
    return values


def resolve_token(args: argparse.Namespace) -> str:
    token = os.environ.get(args.token_env_var)
    if token:
        return token
    if args.token_env_file:
        values = parse_env_file(Path(args.token_env_file))
        token = values.get(args.token_env_var)
        if token:
            return token
    raise RuntimeError(
        f"Missing Slack token. Set {args.token_env_var} in the environment or provide --token-env-file."
    )


def load_pending_records(
    history_path: Path,
    latest_payload_path: Path,
    last_sent_sequence: int,
    latest_only: bool,
) -> list[dict]:
    records: list[dict] = []
    if latest_only:
        if latest_payload_path.exists():
            record = read_json(latest_payload_path)
            if int(record.get("sequence", 0)) > last_sent_sequence:
                records.append(record)
        return records

    if history_path.exists():
        with history_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                sequence = int(record.get("sequence", 0))
                if sequence > last_sent_sequence and "message" in record and "channel_id" in record:
                    records.append(record)
    elif latest_payload_path.exists():
        record = read_json(latest_payload_path)
        if int(record.get("sequence", 0)) > last_sent_sequence:
            records.append(record)

    records.sort(key=lambda item: int(item.get("sequence", 0)))
    return records


def slack_post_message(token: str, channel_id: str, message: str) -> dict:
    payload = json.dumps({"channel": channel_id, "text": message}).encode("utf-8")
    request = urllib.request.Request(
        POST_MESSAGE_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Slack HTTP error {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Slack connection error: {exc.reason}") from exc

    parsed = json.loads(body)
    if not parsed.get("ok"):
        raise RuntimeError(f"Slack API error: {parsed.get('error', 'unknown_error')}")
    return parsed


def main() -> int:
    args = parse_args()
    state_dir = Path(args.state_dir)
    payload_path = Path(args.payload_file) if args.payload_file else state_dir / "latest_payload.json"
    history_path = Path(args.history_file) if args.history_file else state_dir / "history.jsonl"
    last_sent_path = Path(args.last_sent_file) if args.last_sent_file else state_dir / "last_sent_sequence.txt"
    results_path = Path(args.results_file) if args.results_file else state_dir / "send_results.jsonl"

    last_sent_sequence = read_last_sent_sequence(last_sent_path)
    pending = load_pending_records(history_path, payload_path, last_sent_sequence, args.latest_only)

    if not pending:
        print(json.dumps({"status": "noop", "last_sent_sequence": last_sent_sequence, "pending": 0}, sort_keys=True))
        return 0

    token = "" if args.dry_run else resolve_token(args)

    for record in pending:
        sequence = int(record.get("sequence", 0))
        result = {
            "attempted_at": utc_now(),
            "channel_id": record["channel_id"],
            "sequence": sequence,
            "status": "dry-run" if args.dry_run else "ok",
        }
        try:
            if args.dry_run:
                response = {"ok": True, "ts": "dry-run"}
            else:
                response = slack_post_message(token, record["channel_id"], record["message"])
            result["message_ts"] = response.get("ts")
            append_jsonl(results_path, result)
            write_last_sent_sequence(last_sent_path, sequence)
        except Exception as exc:  # pragma: no cover - exercised by runtime failures
            result["status"] = "failed"
            result["error"] = str(exc)
            append_jsonl(results_path, result)
            print(json.dumps(result, sort_keys=True))
            return 1

    print(
        json.dumps(
            {
                "status": "ok" if not args.dry_run else "dry-run",
                "sent": len(pending),
                "last_sent_sequence": int(pending[-1].get("sequence", 0)),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
