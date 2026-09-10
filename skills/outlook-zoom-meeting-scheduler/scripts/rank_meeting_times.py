#!/usr/bin/env python3
"""Rank Outlook getSchedule results using organizer scheduling preferences."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


WINDOWS_TIMEZONES = {
    "Eastern Standard Time": "America/New_York",
    "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver",
    "Pacific Standard Time": "America/Los_Angeles",
    "UTC": "UTC",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("response", type=Path)
    parser.add_argument("--start", required=True, help="Schedule start, local ISO")
    parser.add_argument("--timezone", default="America/New_York")
    parser.add_argument("--organizer", required=True)
    parser.add_argument("--attendee", action="append", required=True)
    parser.add_argument("--duration-minutes", type=int, default=30)
    parser.add_argument("--interval-minutes", type=int, default=30)
    parser.add_argument("--workday-start", default="08:00")
    parser.add_argument("--workday-end", default="17:00")
    parser.add_argument("--avoid-window", action="append", default=[])
    parser.add_argument("--allow-lunch", action="store_true")
    parser.add_argument("--penetrable-subject", action="append", default=[])
    parser.add_argument("--no-default-penetrable", action="store_true")
    parser.add_argument("--require-adjacent", action="store_true")
    return parser.parse_args()


def parse_clock(value: str) -> time:
    return time.fromisoformat(value)


def parse_window(value: str) -> tuple[time, time]:
    start, end = value.split("-", maxsplit=1)
    return parse_clock(start), parse_clock(end)


def parse_event_datetime(value: dict[str, str], local_zone: ZoneInfo) -> datetime:
    parsed = datetime.fromisoformat(value["dateTime"])
    zone_name = WINDOWS_TIMEZONES.get(value.get("timeZone", ""), "")
    zone = ZoneInfo(zone_name) if zone_name else local_zone
    return parsed.replace(tzinfo=zone).astimezone(local_zone)


def overlaps(
    start: datetime, end: datetime, other_start: datetime, other_end: datetime
) -> bool:
    return start < other_end and end > other_start


def overlaps_clock_window(
    start: datetime, end: datetime, blocked_start: time, blocked_end: time
) -> bool:
    return start.time() < blocked_end and end.time() > blocked_start


def main() -> None:
    args = parse_args()
    if args.duration_minutes % args.interval_minutes:
        raise SystemExit("duration must be a multiple of the availability interval")

    local_zone = ZoneInfo(args.timezone)
    schedule_start = datetime.fromisoformat(args.start).replace(tzinfo=local_zone)
    interval = timedelta(minutes=args.interval_minutes)
    covered_slots = args.duration_minutes // args.interval_minutes
    duration = timedelta(minutes=args.duration_minutes)
    workday_start = parse_clock(args.workday_start)
    workday_end = parse_clock(args.workday_end)

    avoid_windows = [parse_window(value) for value in args.avoid_window]
    if not args.allow_lunch:
        avoid_windows.append((time(11, 30), time(13, 0)))

    penetrable = set(args.penetrable_subject)
    if not args.no_default_penetrable:
        penetrable.add("IC Work")

    payload = json.loads(args.response.read_text(encoding="utf-8"))
    entries = {item["scheduleId"].lower(): item for item in payload["value"]}
    organizer = entries[args.organizer.lower()]
    attendee_views = [
        entries[email.lower()]["availabilityView"] for email in args.attendee
    ]

    organizer_events = []
    for item in organizer.get("scheduleItems", []):
        organizer_events.append(
            {
                "subject": item.get("subject", ""),
                "status": item.get("status", ""),
                "start": parse_event_datetime(item["start"], local_zone),
                "end": parse_event_datetime(item["end"], local_zone),
            }
        )

    candidates = []
    view_length = min(len(view) for view in attendee_views)
    for index in range(0, view_length - covered_slots + 1):
        start = schedule_start + index * interval
        end = start + duration

        if start.weekday() >= 5 or start.date() != end.date():
            continue
        if start.time() < workday_start or end.time() > workday_end:
            continue
        if any(
            overlaps_clock_window(start, end, blocked_start, blocked_end)
            for blocked_start, blocked_end in avoid_windows
        ):
            continue
        if any(
            any(view[index + offset] != "0" for offset in range(covered_slots))
            for view in attendee_views
        ):
            continue

        organizer_overlaps = [
            event
            for event in organizer_events
            if event["status"] != "free"
            and overlaps(start, end, event["start"], event["end"])
        ]
        if any(
            event["status"] == "oof" or event["subject"] not in penetrable
            for event in organizer_overlaps
        ):
            continue

        neighbors_after = [
            event["subject"]
            for event in organizer_events
            if event["status"] != "free"
            and event["subject"] not in penetrable
            and event["end"] == start
        ]
        neighbors_before = [
            event["subject"]
            for event in organizer_events
            if event["status"] != "free"
            and event["subject"] not in penetrable
            and event["start"] == end
        ]
        adjacent = bool(neighbors_after or neighbors_before)
        if args.require_adjacent and not adjacent:
            continue

        candidates.append(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "adjacent": adjacent,
                "uses_penetrable_block": bool(organizer_overlaps),
                "after_meetings": neighbors_after,
                "before_meetings": neighbors_before,
            }
        )

    candidates.sort(key=lambda item: (not item["adjacent"], item["start"]))
    print(json.dumps(candidates, indent=2))


if __name__ == "__main__":
    main()
