#!/usr/bin/env python3
"""Prepare a minimal Slack payload for private status updates."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


DEFAULT_CHANNEL_ID = "C0ATGDN0B9Q"
DEFAULT_LABEL = "Status update"
LOCAL_TZ = ZoneInfo("America/New_York")


@dataclass
class LatestSuccess:
    timestamp_local: str
    png_filename: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare a Slack send payload for private updates.")
    parser.add_argument("--channel-id", default=DEFAULT_CHANNEL_ID, help="Slack channel id.")
    parser.add_argument("--label", default=DEFAULT_LABEL, help="Status label prefix.")
    parser.add_argument("--message", help="Exact message text to send.")
    parser.add_argument("--copy-state-dir", help="Path to a Pittston copy-state directory.")
    parser.add_argument("--pid", type=int, help="Optional downloader pid to report health.")
    parser.add_argument("--next-update-minutes", type=int, help="Optional next update cadence in minutes.")
    args = parser.parse_args()
    if bool(args.message) == bool(args.copy_state_dir):
        parser.error("Provide exactly one of --message or --copy-state-dir.")
    return args


def line_count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as handle:
        return sum(1 for _ in handle)


def now_local() -> datetime:
    return datetime.now(tz=LOCAL_TZ)


def format_local(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")


def parse_results_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(LOCAL_TZ)


def load_latest_success(results_path: Path) -> LatestSuccess | None:
    if not results_path.exists():
        return None
    latest: dict | None = None
    with results_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if record.get("status") != "ok":
                continue
            latest = record
    if latest is None:
        return None
    local_ts = parse_results_timestamp(latest["timestamp"])
    return LatestSuccess(
        timestamp_local=format_local(local_ts),
        png_filename=latest.get("png_filename", "unknown"),
    )


def pid_health(pid: int | None) -> str | None:
    if pid is None:
        return None
    return "healthy" if Path(f"/proc/{pid}").exists() else "stopped"


def build_status_message(args: argparse.Namespace) -> str:
    state_dir = Path(args.copy_state_dir)
    copied = line_count(state_dir / "copied_pngs.txt")
    failed = line_count(state_dir / "failed_pngs.txt")
    pending = line_count(state_dir / "pending_pngs.txt")
    latest_success = load_latest_success(state_dir / "results.jsonl")

    lines = [f"{args.label} update at `{format_local(now_local())}`."]
    lines.append(f"`{copied}` copied, `{failed}` failed, `{pending}` pending.")

    health = pid_health(args.pid)
    if health is not None:
        lines.append(f"Downloader: `{health}`.")

    if latest_success is not None:
        lines.append(
            f"Latest success: `{latest_success.timestamp_local}` for `{latest_success.png_filename}`."
        )

    if args.next_update_minutes:
        next_time = now_local() + timedelta(minutes=args.next_update_minutes)
        lines.append(f"Next update expected at about `{format_local(next_time)}`.")

    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    message = args.message if args.message is not None else build_status_message(args)
    payload = {"channel_id": args.channel_id, "message": message}
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
