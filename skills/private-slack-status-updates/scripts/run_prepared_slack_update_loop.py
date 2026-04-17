#!/usr/bin/env python3
"""Generate Slack payloads on a cadence by repeatedly calling prepare_slack_update.py."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate Slack payloads on a cadence.")
    parser.add_argument("--prepare-script", help="Optional explicit path to prepare_slack_update.py.")
    parser.add_argument("--state-dir", required=True, help="Directory where payload state files will be written.")
    parser.add_argument("--interval-seconds", type=int, default=300, help="Cadence in seconds. Default: 300.")
    parser.add_argument("--max-runs", type=int, help="Optional limit for test runs.")
    args, prepare_args = parser.parse_known_args()
    if not prepare_args:
        parser.error("Provide prepare_slack_update.py arguments after the cadence options.")
    args.prepare_args = prepare_args
    return args


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def prepare_script_path(args: argparse.Namespace) -> Path:
    if args.prepare_script:
        return Path(args.prepare_script)
    return Path(__file__).with_name("prepare_slack_update.py")


def cleaned_prepare_args(values: list[str]) -> list[str]:
    if values and values[0] == "--":
        return values[1:]
    return values


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True))
        handle.write("\n")


def read_last_sequence(path: Path) -> int:
    if not path.exists():
        return 0
    text = path.read_text(encoding="utf-8").strip()
    return int(text) if text else 0


def write_payload_files(state_dir: Path, sequence: int, payload: dict) -> None:
    latest_payload = state_dir / "latest_payload.json"
    latest_sequence = state_dir / "latest_sequence.txt"
    history = state_dir / "history.jsonl"
    record = {
        "sequence": sequence,
        "generated_at": utc_now(),
        "channel_id": payload["channel_id"],
        "message": payload["message"],
    }
    latest_payload.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    latest_sequence.write_text(f"{sequence}\n", encoding="utf-8")
    append_jsonl(history, record)


def main() -> int:
    args = parse_args()
    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    prepare_script = prepare_script_path(args)
    prepare_args = cleaned_prepare_args(args.prepare_args)

    run_count = 0
    sequence = read_last_sequence(state_dir / "latest_sequence.txt")

    while True:
        command = [sys.executable, str(prepare_script), *prepare_args]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            failure = {
                "sequence": sequence + 1,
                "generated_at": utc_now(),
                "status": "failed",
                "returncode": completed.returncode,
                "stderr": (completed.stderr or "")[-4000:],
                "stdout": (completed.stdout or "")[-4000:],
                "command": command,
            }
            append_jsonl(state_dir / "history.jsonl", failure)
            print(json.dumps(failure, sort_keys=True))
            return completed.returncode

        payload = json.loads((completed.stdout or "").strip())
        sequence += 1
        write_payload_files(state_dir, sequence, payload)
        print(
            json.dumps(
                {
                    "sequence": sequence,
                    "generated_at": utc_now(),
                    "state_dir": str(state_dir),
                },
                sort_keys=True,
            )
        )
        sys.stdout.flush()

        run_count += 1
        if args.max_runs is not None and run_count >= args.max_runs:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
