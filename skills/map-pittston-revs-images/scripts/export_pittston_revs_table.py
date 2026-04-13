#!/usr/bin/env python3
"""Export Pittston/Washington donor tote to REV image mappings as CSV."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from bisect import bisect_left
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence, TypeVar
from zoneinfo import ZoneInfo

import requests

LOCAL_TZ = ZoneInfo("America/New_York")
METRICS_INDEX = "pit-washington-metric_events*"
LOG_INDEX = "pit-washington-log.records*"
SYNC_LOGGER = "ImageBarcodeCameraUpstreamMessageSynchronizer"
SAVE_LOGGER = "/pick_scanner/perception_logger"
PREDICTION_LOGGER = "ImageBarcodeRobotEligibilityApplication"
PNG_RE = re.compile(r"Saved image topic .*? as (?P<filename>\S+\.png)")
TOTE_RE = re.compile(r"\b([A-Z]:\d{4,}|[A-Z]{1,4}:\d{2,}|R:\d{5}|\w+:\d+)\b")
TOTE_FIELDS = ("tote_id.keyword", "DonorToteId.keyword", "ToteID.keyword")
T = TypeVar("T")


@dataclass(frozen=True)
class PairRow:
    date: str
    donor_tote_id: str
    product_id: str
    pair_first_seen_ts: str


@dataclass(frozen=True)
class SyncEvent:
    ts: datetime
    tote_id: str
    latest_image_timestamp: str


@dataclass(frozen=True)
class SaveEvent:
    ts: datetime
    png_filename: str


@dataclass(frozen=True)
class PredictionEvent:
    ts: datetime
    tote_id: str
    is_eligible: str
    reason: str


@dataclass(frozen=True)
class TimeWindow:
    start: datetime
    end: datetime


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Export a CSV that maps Pittston/Washington donor tote plus product pairs "
            "to REV sync timestamps, perception-logger PNG filenames, and prediction outcomes."
        )
    )
    parser.add_argument("--date", action="append", default=[], help="Single local date in YYYY-MM-DD format. Repeatable.")
    parser.add_argument("--date-from", help="First local date in YYYY-MM-DD format.")
    parser.add_argument("--date-to", help="Last local date in YYYY-MM-DD format.")
    parser.add_argument("--output", required=True, help="Output CSV path.")
    parser.add_argument("--work-dir", help="Directory for intermediate per-stage files. Default: <output>.work")
    parser.add_argument("--slice-minutes", type=int, default=30, help="Sync extraction slice size in minutes. Default: 30.")
    parser.add_argument("--composite-size", type=int, default=1000, help="Composite aggregation page size. Default: 1000.")
    parser.add_argument("--scroll-size", type=int, default=500, help="Scroll page size for log extraction. Default: 500.")
    parser.add_argument("--tote-batch-size", type=int, default=400, help="Tote term batch size for sync lookups. Default: 400.")
    parser.add_argument("--bucket-seconds", type=int, default=60, help="Bucket size in seconds for local save/prediction window compilation. Default: 60.")
    parser.add_argument("--save-window-seconds", type=float, default=5.0, help="Maximum delta between sync and PNG save event. Default: 5 seconds.")
    parser.add_argument("--prediction-window-seconds", type=float, default=30.0, help="Maximum delta between sync and prediction event. Default: 30 seconds.")
    parser.add_argument("--sync-window-hours", type=float, default=12.0, help="Maximum delta between pair timestamp and sync event for the same tote. Default: 12 hours.")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds. Default: 30.")
    args = parser.parse_args()
    if not args.date and not (args.date_from and args.date_to):
        parser.error("Provide either --date or both --date-from and --date-to.")
    if (args.date_from and not args.date_to) or (args.date_to and not args.date_from):
        parser.error("Use --date-from and --date-to together.")
    if args.slice_minutes <= 0:
        parser.error("--slice-minutes must be positive.")
    if args.bucket_seconds <= 0:
        parser.error("--bucket-seconds must be positive.")
    return args


def parse_local_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def iter_dates(args: argparse.Namespace) -> list[date]:
    values = {parse_local_date(raw) for raw in args.date}
    if args.date_from and args.date_to:
        start = parse_local_date(args.date_from)
        end = parse_local_date(args.date_to)
        if end < start:
            raise SystemExit("--date-to must be on or after --date-from")
        current = start
        while current <= end:
            values.add(current)
            current += timedelta(days=1)
    return sorted(values)


def utc_bounds_for_day(day: date) -> tuple[datetime, datetime]:
    local_start = datetime.combine(day, time.min, tzinfo=LOCAL_TZ)
    local_end = local_start + timedelta(days=1)
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_ts(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def get_es_credentials() -> tuple[str, tuple[str, str]]:
    from bg_vault_elastic.client import VaultElasticClient

    client = VaultElasticClient()
    creds = client.get_es_credentials("elastic-washington-cluster")
    return creds["url"].rstrip("/"), (creds["username"], creds["password"])


def es_post(session: requests.Session, url: str, auth: tuple[str, str], body: dict, timeout: int) -> dict:
    response = session.post(url, auth=auth, json=body, headers={"Accept": "application/json"}, timeout=timeout)
    if not response.ok:
        raise RuntimeError(f"Elasticsearch request failed: {response.status_code} {response.text[:500]}")
    return response.json()


def es_delete(session: requests.Session, url: str, auth: tuple[str, str], body: dict, timeout: int) -> None:
    response = session.delete(url, auth=auth, json=body, headers={"Accept": "application/json"}, timeout=timeout)
    if not response.ok and response.status_code != 404:
        raise RuntimeError(f"Elasticsearch cleanup failed: {response.status_code} {response.text[:500]}")


def scroll_search(
    session: requests.Session,
    base_url: str,
    auth: tuple[str, str],
    index: str,
    body: dict,
    timeout: int,
) -> Iterable[dict]:
    start = session.post(
        f"{base_url}/{index}/_search?scroll=2m",
        auth=auth,
        json=body,
        headers={"Accept": "application/json"},
        timeout=timeout,
    )
    if not start.ok:
        raise RuntimeError(f"Elasticsearch scroll start failed: {start.status_code} {start.text[:500]}")
    payload = start.json()
    scroll_id = payload.get("_scroll_id")
    try:
        while True:
            hits = payload.get("hits", {}).get("hits", [])
            if not hits:
                return
            for hit in hits:
                yield hit
            payload = es_post(
                session,
                f"{base_url}/_search/scroll",
                auth,
                {"scroll": "2m", "scroll_id": scroll_id},
                timeout,
            )
            scroll_id = payload.get("_scroll_id", scroll_id)
    finally:
        if scroll_id:
            es_delete(session, f"{base_url}/_search/scroll", auth, {"scroll_id": [scroll_id]}, timeout)


def chunked(values: Sequence[T], size: int) -> Iterable[list[T]]:
    for index in range(0, len(values), size):
        yield list(values[index : index + size])


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True))
            handle.write("\n")


def serialize_sync(event: SyncEvent) -> dict:
    return {
        "ts": iso_z(event.ts),
        "tote_id": event.tote_id,
        "latest_image_timestamp": event.latest_image_timestamp,
    }


def serialize_save(event: SaveEvent) -> dict:
    return {"ts": iso_z(event.ts), "png_filename": event.png_filename}


def serialize_prediction(event: PredictionEvent) -> dict:
    return {
        "ts": iso_z(event.ts),
        "tote_id": event.tote_id,
        "is_eligible": event.is_eligible,
        "reason": event.reason,
    }


def write_pairs_csv(path: Path, pairs: list[PairRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["date", "donor_tote_id", "product_id", "pair_first_seen_ts"])
        writer.writeheader()
        for pair in pairs:
            writer.writerow(asdict(pair))


def fetch_pickcomplete_pairs(
    session: requests.Session,
    base_url: str,
    auth: tuple[str, str],
    day: date,
    composite_size: int,
    timeout: int,
) -> list[PairRow]:
    start_utc, end_utc = utc_bounds_for_day(day)
    after_key: dict[str, str] | None = None
    rows: list[PairRow] = []
    while True:
        composite: dict[str, object] = {
            "size": composite_size,
            "sources": [
                {"donor_tote_id": {"terms": {"field": "DonorToteId.keyword"}}},
                {"product_id": {"terms": {"field": "ProductId.keyword"}}},
            ],
        }
        if after_key:
            composite["after"] = after_key
        body = {
            "size": 0,
            "query": {
                "bool": {
                    "filter": [
                        {"range": {"@timestamp": {"gte": iso_z(start_utc), "lt": iso_z(end_utc)}}},
                        {"term": {"EventType.keyword": "PickComplete"}},
                        {"exists": {"field": "DonorToteId.keyword"}},
                        {"exists": {"field": "ProductId.keyword"}},
                    ]
                }
            },
            "aggs": {
                "pairs": {
                    "composite": composite,
                    "aggs": {
                        "pair_first_seen_ts": {
                            "min": {"field": "@timestamp", "format": "strict_date_time"}
                        }
                    },
                }
            },
        }
        data = es_post(session, f"{base_url}/{METRICS_INDEX}/_search", auth, body, timeout)
        pairs = data["aggregations"]["pairs"]
        for bucket in pairs["buckets"]:
            rows.append(
                PairRow(
                    date=day.isoformat(),
                    donor_tote_id=bucket["key"]["donor_tote_id"],
                    product_id=bucket["key"]["product_id"],
                    pair_first_seen_ts=bucket["pair_first_seen_ts"].get("value_as_string", ""),
                )
            )
        after_key = pairs.get("after_key")
        if not after_key:
            return rows


def extract_tote_id(source: dict) -> str | None:
    for key in ("tote_id", "DonorToteId", "ToteID"):
        value = source.get(key)
        if isinstance(value, str) and value:
            return value
    message = source.get("message") or source.get("msg") or ""
    match = TOTE_RE.search(message)
    return match.group(1) if match else None


def parse_sync_event(hit: dict) -> SyncEvent | None:
    source = hit.get("_source", {})
    tote_id = extract_tote_id(source)
    latest_image_timestamp = source.get("latest_image_timestamp")
    if not tote_id or not latest_image_timestamp:
        return None
    return SyncEvent(ts=parse_ts(source["@timestamp"]), tote_id=tote_id, latest_image_timestamp=str(latest_image_timestamp))


def parse_save_event(hit: dict) -> SaveEvent | None:
    source = hit.get("_source", {})
    message = source.get("message") or source.get("msg") or ""
    match = PNG_RE.search(message)
    if not match:
        return None
    return SaveEvent(ts=parse_ts(source["@timestamp"]), png_filename=match.group("filename"))


def parse_prediction_event(hit: dict) -> PredictionEvent | None:
    source = hit.get("_source", {})
    tote_id = extract_tote_id(source)
    if not tote_id:
        return None
    return PredictionEvent(
        ts=parse_ts(source["@timestamp"]),
        tote_id=tote_id,
        is_eligible=str(source.get("is_eligible", "")),
        reason=str(source.get("reason", "")),
    )


def make_tote_filter(totes: Sequence[str]) -> dict:
    return {
        "bool": {
            "should": [{"terms": {field: list(totes)}} for field in TOTE_FIELDS],
            "minimum_should_match": 1,
        }
    }


def fetch_sync_events_for_day(
    session: requests.Session,
    base_url: str,
    auth: tuple[str, str],
    day: date,
    tote_ids: Sequence[str],
    slice_minutes: int,
    tote_batch_size: int,
    scroll_size: int,
    timeout: int,
) -> list[SyncEvent]:
    day_start, day_end = utc_bounds_for_day(day)
    slice_delta = timedelta(minutes=slice_minutes)
    results: list[SyncEvent] = []
    tote_batches = list(chunked(sorted(set(tote_ids)), tote_batch_size))

    window_start = day_start
    while window_start < day_end:
        window_end = min(window_start + slice_delta, day_end)
        print(f"  sync slice {iso_z(window_start)} -> {iso_z(window_end)} across {len(tote_batches)} tote batches", file=sys.stderr)
        for batch in tote_batches:
            body = {
                "size": scroll_size,
                "sort": [{"@timestamp": "asc"}],
                "query": {
                    "bool": {
                        "filter": [
                            {"range": {"@timestamp": {"gte": iso_z(window_start), "lt": iso_z(window_end)}}},
                            {"term": {"logger.keyword": SYNC_LOGGER}},
                            make_tote_filter(batch),
                        ]
                    }
                },
            }
            for hit in scroll_search(session, base_url, auth, LOG_INDEX, body, timeout):
                event = parse_sync_event(hit)
                if event is not None:
                    results.append(event)
        window_start = window_end
    results.sort(key=lambda item: item.ts)
    return results


def floor_timestamp(value: datetime, bucket_seconds: int) -> datetime:
    epoch = int(value.timestamp())
    floored = epoch - (epoch % bucket_seconds)
    return datetime.fromtimestamp(floored, tz=timezone.utc)


def merge_windows(windows: list[TimeWindow]) -> list[TimeWindow]:
    if not windows:
        return []
    ordered = sorted(windows, key=lambda item: item.start)
    merged = [ordered[0]]
    for window in ordered[1:]:
        current = merged[-1]
        if window.start <= current.end:
            merged[-1] = TimeWindow(start=current.start, end=max(current.end, window.end))
        else:
            merged.append(window)
    return merged


def build_bucketed_windows(
    sync_events: Sequence[SyncEvent],
    lead_seconds: float,
    lag_seconds: float,
    bucket_seconds: int,
) -> list[TimeWindow]:
    if not sync_events:
        return []
    lead = timedelta(seconds=lead_seconds)
    lag = timedelta(seconds=lag_seconds)
    bucket_delta = timedelta(seconds=bucket_seconds)
    raw_windows: list[TimeWindow] = []
    seen_buckets: set[datetime] = set()
    for event in sync_events:
        bucket_start = floor_timestamp(event.ts, bucket_seconds)
        if bucket_start in seen_buckets:
            continue
        seen_buckets.add(bucket_start)
        raw_windows.append(TimeWindow(start=bucket_start - lead, end=bucket_start + bucket_delta + lag))
    return merge_windows(raw_windows)


def fetch_windowed_events(
    session: requests.Session,
    base_url: str,
    auth: tuple[str, str],
    windows: Sequence[TimeWindow],
    logger_name: str,
    parser: Callable[[dict], T | None],
    scroll_size: int,
    timeout: int,
    stage_label: str,
) -> list[T]:
    results: list[T] = []
    for index, window in enumerate(windows, start=1):
        print(f"  {stage_label} window {index}/{len(windows)}: {iso_z(window.start)} -> {iso_z(window.end)}", file=sys.stderr)
        body = {
            "size": scroll_size,
            "sort": [{"@timestamp": "asc"}],
            "query": {
                "bool": {
                    "filter": [
                        {"range": {"@timestamp": {"gte": iso_z(window.start), "lt": iso_z(window.end)}}},
                        {"term": {"logger.keyword": logger_name}},
                    ]
                }
            },
        }
        for hit in scroll_search(session, base_url, auth, LOG_INDEX, body, timeout):
            event = parser(hit)
            if event is not None:
                results.append(event)
    results.sort(key=lambda item: item.ts)
    return results


def group_syncs_by_tote(sync_events: Sequence[SyncEvent]) -> tuple[dict[str, list[SyncEvent]], dict[str, list[datetime]]]:
    events_by_tote: dict[str, list[SyncEvent]] = {}
    timestamps_by_tote: dict[str, list[datetime]] = {}
    for event in sync_events:
        events_by_tote.setdefault(event.tote_id, []).append(event)
        timestamps_by_tote.setdefault(event.tote_id, []).append(event.ts)
    return events_by_tote, timestamps_by_tote


def group_predictions_by_tote(prediction_events: Sequence[PredictionEvent]) -> tuple[dict[str, list[PredictionEvent]], dict[str, list[datetime]]]:
    events_by_tote: dict[str, list[PredictionEvent]] = {}
    timestamps_by_tote: dict[str, list[datetime]] = {}
    for event in prediction_events:
        events_by_tote.setdefault(event.tote_id, []).append(event)
        timestamps_by_tote.setdefault(event.tote_id, []).append(event.ts)
    return events_by_tote, timestamps_by_tote


def nearest_by_timestamp(
    events: Sequence[T],
    timestamps: Sequence[datetime],
    target: datetime,
    max_delta: timedelta | None,
) -> T | None:
    if not events:
        return None
    position = bisect_left(timestamps, target)
    candidates: list[T] = []
    if position < len(events):
        candidates.append(events[position])
    if position > 0:
        candidates.append(events[position - 1])
    if not candidates:
        return None
    chosen = min(candidates, key=lambda event: abs(event.ts - target))
    if max_delta is not None and abs(chosen.ts - target) > max_delta:
        return None
    return chosen


def build_rows_for_day(
    pairs: list[PairRow],
    syncs_by_tote: dict[str, list[SyncEvent]],
    sync_timestamps_by_tote: dict[str, list[datetime]],
    save_events: list[SaveEvent],
    predictions_by_tote: dict[str, list[PredictionEvent]],
    prediction_timestamps_by_tote: dict[str, list[datetime]],
    save_window_seconds: float,
    prediction_window_seconds: float,
    sync_window_hours: float,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    save_timestamps = [event.ts for event in save_events]
    sync_limit = timedelta(hours=sync_window_hours)
    save_limit = timedelta(seconds=save_window_seconds)
    prediction_limit = timedelta(seconds=prediction_window_seconds)

    for pair in pairs:
        first_seen_ts = parse_ts(pair.pair_first_seen_ts)
        sync_event = nearest_by_timestamp(
            syncs_by_tote.get(pair.donor_tote_id, []),
            sync_timestamps_by_tote.get(pair.donor_tote_id, []),
            first_seen_ts,
            sync_limit,
        )
        save_event = None
        prediction_event = None
        if sync_event is not None:
            save_event = nearest_by_timestamp(save_events, save_timestamps, sync_event.ts, save_limit)
            prediction_event = nearest_by_timestamp(
                predictions_by_tote.get(pair.donor_tote_id, []),
                prediction_timestamps_by_tote.get(pair.donor_tote_id, []),
                sync_event.ts,
                prediction_limit,
            )
        rows.append(
            {
                "date": pair.date,
                "donor_tote_id": pair.donor_tote_id,
                "product_id": pair.product_id,
                "pair_first_seen_ts": pair.pair_first_seen_ts,
                "sync_ts": iso_z(sync_event.ts) if sync_event else "",
                "latest_image_timestamp": sync_event.latest_image_timestamp if sync_event else "",
                "png_filename": save_event.png_filename if save_event else "",
                "prediction_ts": iso_z(prediction_event.ts) if prediction_event else "",
                "is_eligible": prediction_event.is_eligible if prediction_event else "",
                "reason": prediction_event.reason if prediction_event else "",
            }
        )
    return rows


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "date",
        "donor_tote_id",
        "product_id",
        "pair_first_seen_ts",
        "sync_ts",
        "latest_image_timestamp",
        "png_filename",
        "prediction_ts",
        "is_eligible",
        "reason",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def default_work_dir(output_path: Path) -> Path:
    return output_path.parent / f"{output_path.name}.work"


def main() -> int:
    args = parse_args()
    dates = iter_dates(args)
    output_path = Path(args.output)
    work_dir = Path(args.work_dir) if args.work_dir else default_work_dir(output_path)
    base_url, auth = get_es_credentials()
    session = requests.Session()
    all_rows: list[dict[str, str]] = []

    for day in dates:
        print(f"Processing {day.isoformat()}...", file=sys.stderr)
        day_dir = work_dir / day.isoformat()
        day_dir.mkdir(parents=True, exist_ok=True)

        pairs = fetch_pickcomplete_pairs(session, base_url, auth, day, args.composite_size, args.timeout)
        write_pairs_csv(day_dir / "pairs.csv", pairs)
        tote_ids = sorted({pair.donor_tote_id for pair in pairs})
        print(f"  pairs: {len(pairs)} rows across {len(tote_ids)} donor totes", file=sys.stderr)

        sync_events = fetch_sync_events_for_day(
            session=session,
            base_url=base_url,
            auth=auth,
            day=day,
            tote_ids=tote_ids,
            slice_minutes=args.slice_minutes,
            tote_batch_size=args.tote_batch_size,
            scroll_size=args.scroll_size,
            timeout=args.timeout,
        )
        write_jsonl(day_dir / "sync_events.jsonl", (serialize_sync(event) for event in sync_events))
        print(f"  sync events: {len(sync_events)}", file=sys.stderr)

        save_windows = build_bucketed_windows(
            sync_events,
            lead_seconds=args.save_window_seconds,
            lag_seconds=args.save_window_seconds,
            bucket_seconds=args.bucket_seconds,
        )
        write_jsonl(day_dir / "save_windows.jsonl", ({"start": iso_z(window.start), "end": iso_z(window.end)} for window in save_windows))
        save_events = fetch_windowed_events(
            session=session,
            base_url=base_url,
            auth=auth,
            windows=save_windows,
            logger_name=SAVE_LOGGER,
            parser=parse_save_event,
            scroll_size=args.scroll_size,
            timeout=args.timeout,
            stage_label="save",
        )
        write_jsonl(day_dir / "save_events.jsonl", (serialize_save(event) for event in save_events))
        print(f"  save events: {len(save_events)} from {len(save_windows)} local windows", file=sys.stderr)

        prediction_windows = build_bucketed_windows(
            sync_events,
            lead_seconds=args.save_window_seconds,
            lag_seconds=args.prediction_window_seconds,
            bucket_seconds=args.bucket_seconds,
        )
        write_jsonl(day_dir / "prediction_windows.jsonl", ({"start": iso_z(window.start), "end": iso_z(window.end)} for window in prediction_windows))
        prediction_events = fetch_windowed_events(
            session=session,
            base_url=base_url,
            auth=auth,
            windows=prediction_windows,
            logger_name=PREDICTION_LOGGER,
            parser=parse_prediction_event,
            scroll_size=args.scroll_size,
            timeout=args.timeout,
            stage_label="prediction",
        )
        write_jsonl(day_dir / "prediction_events.jsonl", (serialize_prediction(event) for event in prediction_events))
        print(f"  prediction events: {len(prediction_events)} from {len(prediction_windows)} local windows", file=sys.stderr)

        syncs_by_tote, sync_timestamps_by_tote = group_syncs_by_tote(sync_events)
        predictions_by_tote, prediction_timestamps_by_tote = group_predictions_by_tote(prediction_events)
        day_rows = build_rows_for_day(
            pairs=pairs,
            syncs_by_tote=syncs_by_tote,
            sync_timestamps_by_tote=sync_timestamps_by_tote,
            save_events=save_events,
            predictions_by_tote=predictions_by_tote,
            prediction_timestamps_by_tote=prediction_timestamps_by_tote,
            save_window_seconds=args.save_window_seconds,
            prediction_window_seconds=args.prediction_window_seconds,
            sync_window_hours=args.sync_window_hours,
        )
        write_csv(day_dir / "joined.csv", day_rows)
        all_rows.extend(day_rows)
        print(f"  joined rows: {len(day_rows)}", file=sys.stderr)

    write_csv(output_path, all_rows)
    print(json.dumps({"output": str(output_path), "work_dir": str(work_dir), "rows": len(all_rows)}, indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
