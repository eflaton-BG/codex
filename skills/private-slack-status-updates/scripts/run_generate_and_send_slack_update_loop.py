#!/usr/bin/env python3
"""Generate Slack payloads and send them directly to Slack on a fixed cadence."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate and send Slack updates on a cadence.")
    parser.add_argument("--generator-script", help="Optional explicit path to run_prepared_slack_update_loop.py.")
    parser.add_argument("--sender-script", help="Optional explicit path to send_prepared_slack_update.py.")
    parser.add_argument("--state-dir", required=True, help="Directory where payload state files will be written.")
    parser.add_argument("--interval-seconds", type=int, default=300, help="Cadence in seconds. Default: 300.")
    parser.add_argument("--max-runs", type=int, help="Optional limit for test runs.")
    parser.add_argument("--token-env-var", default="SLACK_BOT_TOKEN", help="Slack token environment variable name.")
    parser.add_argument("--token-env-file", help="Optional env file containing the Slack token.")
    parser.add_argument("--dry-run-send", action="store_true", help="Do not call Slack; record dry-run send results.")
    args, generator_args = parser.parse_known_args()
    if not generator_args:
        parser.error("Provide payload-generation arguments after the cadence options.")
    args.generator_args = generator_args
    return args


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def cleaned_args(values: list[str]) -> list[str]:
    if values and values[0] == "--":
        return values[1:]
    return values


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True))
        handle.write("\n")


def script_path(explicit: str | None, name: str) -> Path:
    if explicit:
        return Path(explicit)
    return Path(__file__).with_name(name)


def run_command(command: list[str], log_path: Path) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    record = {
        "completed_at": utc_now(),
        "command": command,
        "returncode": completed.returncode,
        "stdout": (completed.stdout or "")[-4000:],
        "stderr": (completed.stderr or "")[-4000:],
    }
    append_jsonl(log_path, record)
    return completed


def main() -> int:
    args = parse_args()
    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    command_log = state_dir / "standalone_command_results.jsonl"

    generator_script = script_path(args.generator_script, "run_prepared_slack_update_loop.py")
    sender_script = script_path(args.sender_script, "send_prepared_slack_update.py")
    generator_args = cleaned_args(args.generator_args)

    run_count = 0
    while True:
        generator_command = [
            sys.executable,
            str(generator_script),
            "--state-dir",
            str(state_dir),
            "--interval-seconds",
            str(args.interval_seconds),
            "--max-runs",
            "1",
            *generator_args,
        ]
        completed = run_command(generator_command, command_log)
        if completed.returncode != 0:
            return completed.returncode

        sender_command = [
            sys.executable,
            str(sender_script),
            "--state-dir",
            str(state_dir),
            "--token-env-var",
            args.token_env_var,
        ]
        if args.token_env_file:
            sender_command.extend(["--token-env-file", args.token_env_file])
        if args.dry_run_send:
            sender_command.append("--dry-run")

        completed = run_command(sender_command, command_log)
        if completed.returncode != 0:
            return completed.returncode

        run_count += 1
        if args.max_runs is not None and run_count >= args.max_runs:
            return 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
