#!/usr/bin/env python3
"""Export one validated S3 image per RES1 SKU from Atlas and ES logs."""

from __future__ import annotations

import argparse
from bisect import bisect_left
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta, timezone
import json
import os
from pathlib import Path
import re
import socket
import sys
import threading
from urllib.parse import quote_plus, urlparse
from zoneinfo import ZoneInfo

import boto3
from botocore.config import Config
import dns.resolver
from pymongo import MongoClient
import requests


LOCAL_TZ = ZoneInfo("America/New_York")
DEFAULT_DATABASE = "washington_pit_washington_operational"
DEFAULT_COLLECTION = "robot_eligibility_prediction_stats"
DEFAULT_ES_SECRET = "elastic-washington-cluster"
DEFAULT_LOG_INDEX = "pit-washington-log.records*"
DEFAULT_S3_PREFIX = "s3://washington-data/pittston/res1/image_data/"
SYNC_LOGGER = "ImageBarcodeCameraUpstreamMessageSynchronizer"
SAVE_LOGGER = "/pick_scanner/perception_logger"
SAVE_RE = re.compile(
    r"Saved image topic /pick_scanner/rgb_camera/raw/image as "
    r"(?P<filename>\S+\.png)"
)
PREDICTION_FIELDS = (
    "sku_id",
    "tote_id",
    "date_created",
    "image_msg_timestamp",
)
SYNC_FIELDS = ("sync_timestamp", "tote_id", "latest_image_timestamp")
SAVE_FIELDS = ("save_timestamp", "png_filename")
CANDIDATE_FIELDS = (
    "sku_id",
    "tote_id",
    "prediction_timestamp",
    "image_msg_timestamp",
    "sync_timestamp",
    "latest_image_timestamp",
    "save_timestamp",
    "png_filename",
    "s3_uri",
)
OUTPUT_FIELDS = CANDIDATE_FIELDS + (
    "s3_size_bytes",
    "s3_etag",
    "selection_reason",
)


def parse_datetime(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso_z(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def compute_window(args: argparse.Namespace) -> tuple[datetime, datetime]:
    if args.start_utc:
        start = parse_datetime(args.start_utc)
    else:
        start_day = date.fromisoformat(args.date_from)
        start = datetime.combine(
            start_day,
            time.min,
            tzinfo=LOCAL_TZ,
        ).astimezone(timezone.utc)

    if args.end_utc:
        end = parse_datetime(args.end_utc)
    else:
        end_day = date.fromisoformat(args.date_to) + timedelta(days=1)
        end = datetime.combine(
            end_day,
            time.min,
            tzinfo=LOCAL_TZ,
        ).astimezone(timezone.utc)

    if end <= start:
        raise ValueError("The end of the requested window must follow the start")
    return start, end


def iter_local_slices(
    start: datetime,
    end: datetime,
) -> list[tuple[date, datetime, datetime]]:
    first_day = start.astimezone(LOCAL_TZ).date()
    last_day = (end - timedelta(microseconds=1)).astimezone(LOCAL_TZ).date()
    result: list[tuple[date, datetime, datetime]] = []
    day = first_day
    while day <= last_day:
        day_start = datetime.combine(
            day,
            time.min,
            tzinfo=LOCAL_TZ,
        ).astimezone(timezone.utc)
        next_start = datetime.combine(
            day + timedelta(days=1),
            time.min,
            tzinfo=LOCAL_TZ,
        ).astimezone(timezone.utc)
        result.append((day, max(start, day_start), min(end, next_start)))
        day += timedelta(days=1)
    return result


def ensure_not_temporary(path: Path) -> None:
    resolved = path.expanduser().resolve()
    if resolved == Path("/tmp") or Path("/tmp") in resolved.parents:
        raise ValueError(
            "Durable export output cannot be under /tmp; use ~/Downloads "
            "or another persistent location"
        )


def write_csv_atomic(
    path: Path,
    fieldnames: tuple[str, ...],
    rows,
) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.part-{os.getpid()}")
    count = 0
    with temporary.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
            count += 1
    os.replace(temporary, path)
    return count


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def count_csv_rows(path: Path) -> int:
    with path.open(encoding="utf-8") as stream:
        return max(sum(1 for _ in stream) - 1, 0)


def iter_csv_rows(paths: list[Path]):
    for path in paths:
        with path.open(newline="", encoding="utf-8") as stream:
            yield from csv.DictReader(stream)


def iter_time_chunks(
    start: datetime,
    end: datetime,
    hours: float,
) -> list[tuple[datetime, datetime]]:
    delta = timedelta(hours=hours)
    chunks: list[tuple[datetime, datetime]] = []
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + delta, end)
        chunks.append((chunk_start, chunk_end))
        chunk_start = chunk_end
    return chunks


def chunk_path(directory: Path, start: datetime, end: datetime) -> Path:
    start_name = start.strftime("%Y%m%dT%H%M%S.%fZ")
    end_name = end.strftime("%Y%m%dT%H%M%S.%fZ")
    return directory / f"{start_name}--{end_name}.csv"


def choose_reachable_private_link(seed_host: str) -> tuple[str, str]:
    records = list(
        dns.resolver.resolve(f"_mongodb._tcp.{seed_host}", "SRV")
    )
    if not records:
        raise RuntimeError(f"No MongoDB SRV records found for {seed_host}")
    target = str(records[0].target).rstrip(".")
    ports = sorted({int(record.port) for record in records})
    original_getaddrinfo = socket.getaddrinfo
    addresses = original_getaddrinfo(
        target,
        ports[0],
        type=socket.SOCK_STREAM,
    )
    for address in addresses:
        ip = address[4][0]
        if all(tcp_reachable(ip, port) for port in ports):
            return target, ip
    raise RuntimeError(
        f"No Atlas private-link address can reach every SRV port for {target}"
    )


def tcp_reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


def patch_atlas_resolution(target: str, reachable_ip: str) -> None:
    original_getaddrinfo = socket.getaddrinfo

    def filtered_getaddrinfo(
        host: str,
        port: int,
        family: int = 0,
        type: int = 0,
        proto: int = 0,
        flags: int = 0,
    ):
        rows = original_getaddrinfo(
            host,
            port,
            family,
            type,
            proto,
            flags,
        )
        if host == target:
            rows = [row for row in rows if row[4][0] == reachable_ip]
        return rows

    socket.getaddrinfo = filtered_getaddrinfo


def atlas_collection(args: argparse.Namespace):
    required = (
        "MONGO_HOST",
        "MONGO_USERNAME",
        "MONGO_PASSWORD",
        "MONGO_DATABASE",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError(
            "Missing keyring-injected MongoDB variables: "
            + ", ".join(missing)
        )

    host = os.environ["MONGO_HOST"]
    target, reachable_ip = choose_reachable_private_link(host)
    patch_atlas_resolution(target, reachable_ip)
    username = quote_plus(os.environ["MONGO_USERNAME"])
    password = quote_plus(os.environ["MONGO_PASSWORD"])
    client = MongoClient(
        f"mongodb+srv://{username}:{password}@{host}/",
        tz_aware=True,
        serverSelectionTimeoutMS=30_000,
    )
    database_name = os.environ.get("MONGO_DATABASE", DEFAULT_DATABASE)
    collection = client[database_name][args.mongo_collection]
    collection.find_one({}, projection={"_id": 1})
    return collection, target, reachable_ip


def export_predictions(
    collection,
    path: Path,
    start: datetime,
    end: datetime,
) -> int:
    if path.is_file():
        return sum(1 for _ in path.open(encoding="utf-8")) - 1

    query = {"date_created": {"$gte": start, "$lt": end}}
    projection = {
        "_id": 0,
        "sku_id": 1,
        "tote_id": 1,
        "date_created": 1,
        "image_msg_timestamp": 1,
    }
    cursor = (
        collection.find(
            query,
            projection=projection,
            batch_size=5000,
            no_cursor_timeout=True,
        )
        .sort("date_created", 1)
    )

    def rows():
        try:
            for document in cursor:
                sku = str(document.get("sku_id") or "").strip()
                tote = str(document.get("tote_id") or "").strip()
                created = document.get("date_created")
                image = document.get("image_msg_timestamp")
                if not sku or not tote or not created or not image:
                    continue
                yield {
                    "sku_id": sku,
                    "tote_id": tote,
                    "date_created": iso_z(created),
                    "image_msg_timestamp": iso_z(image),
                }
        finally:
            cursor.close()

    return write_csv_atomic(path, PREDICTION_FIELDS, rows())


def es_credentials(args: argparse.Namespace) -> tuple[str, tuple[str, str]]:
    from bg_vault_elastic.client import VaultElasticClient

    credentials = VaultElasticClient().get_es_credentials(args.es_secret)
    return credentials["url"].rstrip("/"), (
        credentials["username"],
        credentials["password"],
    )


def es_scroll(
    session: requests.Session,
    base: str,
    auth: tuple[str, str],
    index: str,
    body: dict,
    timeout: int,
):
    response = session.post(
        f"{base}/{index}/_search?scroll=3m",
        auth=auth,
        json=body,
        timeout=timeout,
    )
    if not response.ok:
        raise RuntimeError(
            f"Elasticsearch scroll start failed: "
            f"{response.status_code} {response.text[:1000]}"
        )
    payload = response.json()
    scroll_id = payload.get("_scroll_id")
    try:
        while True:
            hits = payload.get("hits", {}).get("hits", [])
            if not hits:
                break
            yield from hits
            response = session.post(
                f"{base}/_search/scroll",
                auth=auth,
                json={"scroll": "3m", "scroll_id": scroll_id},
                timeout=timeout,
            )
            if not response.ok:
                raise RuntimeError(
                    f"Elasticsearch scroll failed: "
                    f"{response.status_code} {response.text[:1000]}"
                )
            payload = response.json()
            scroll_id = payload.get("_scroll_id", scroll_id)
    finally:
        if scroll_id:
            session.delete(
                f"{base}/_search/scroll",
                auth=auth,
                json={"scroll_id": [scroll_id]},
                timeout=timeout,
            )


def export_syncs(
    session: requests.Session,
    base: str,
    auth: tuple[str, str],
    args: argparse.Namespace,
    path: Path,
    start: datetime,
    end: datetime,
) -> int:
    if path.is_file():
        return count_csv_rows(path)
    body = {
        "size": args.es_page_size,
        "_source": [
            "@timestamp",
            "tote_id",
            "DonorToteId",
            "ToteID",
            "latest_image_timestamp",
        ],
        "sort": [{"@timestamp": "asc"}],
        "query": {
            "bool": {
                "filter": [
                    {
                        "range": {
                            "@timestamp": {
                                "gte": "",
                                "lt": "",
                            }
                        }
                    },
                    {"term": {"logger.keyword": SYNC_LOGGER}},
                    {"term": {"system_name.keyword": "res1"}},
                ]
            }
        },
    }

    def rows(chunk_start: datetime, chunk_end: datetime):
        body["query"]["bool"]["filter"][0]["range"]["@timestamp"] = {
            "gte": iso_z(chunk_start),
            "lt": iso_z(chunk_end),
        }
        for hit in es_scroll(
            session,
            base,
            auth,
            args.log_index,
            body,
            args.timeout,
        ):
            source = hit.get("_source", {})
            tote = next(
                (
                    str(source.get(field)).strip()
                    for field in ("tote_id", "DonorToteId", "ToteID")
                    if source.get(field)
                ),
                "",
            )
            latest = source.get("latest_image_timestamp")
            timestamp = source.get("@timestamp")
            if tote and latest and timestamp:
                yield {
                    "sync_timestamp": iso_z(parse_datetime(str(timestamp))),
                    "tote_id": tote,
                    "latest_image_timestamp": iso_z(
                        parse_datetime(str(latest))
                    ),
                }

    expanded_start = start - timedelta(seconds=10)
    expanded_end = end + timedelta(seconds=10)
    parts_dir = path.parent / f"{path.stem}.parts"
    parts: list[Path] = []
    for chunk_start, chunk_end in iter_time_chunks(
        expanded_start,
        expanded_end,
        args.es_slice_hours,
    ):
        part = chunk_path(parts_dir, chunk_start, chunk_end)
        parts.append(part)
        if part.is_file():
            continue
        print(
            f"    sync slice {iso_z(chunk_start)} -> "
            f"{iso_z(chunk_end)}",
            flush=True,
        )
        write_csv_atomic(
            part,
            SYNC_FIELDS,
            rows(chunk_start, chunk_end),
        )
    return write_csv_atomic(
        path,
        SYNC_FIELDS,
        iter_csv_rows(parts),
    )


def export_saves(
    session: requests.Session,
    base: str,
    auth: tuple[str, str],
    args: argparse.Namespace,
    path: Path,
    start: datetime,
    end: datetime,
) -> int:
    if path.is_file():
        return count_csv_rows(path)
    body = {
        "size": args.es_page_size,
        "_source": ["@timestamp", "message"],
        "sort": [{"@timestamp": "asc"}],
        "query": {
            "bool": {
                "filter": [
                    {
                        "range": {
                            "@timestamp": {
                                "gte": "",
                                "lt": "",
                            }
                        }
                    },
                    {"term": {"logger.keyword": SAVE_LOGGER}},
                    {"term": {"system_name.keyword": "res1"}},
                ],
                "must": [
                    {"match_phrase": {"message": "Saved image topic"}}
                ],
            }
        },
    }

    def rows(chunk_start: datetime, chunk_end: datetime):
        body["query"]["bool"]["filter"][0]["range"]["@timestamp"] = {
            "gte": iso_z(chunk_start),
            "lt": iso_z(chunk_end),
        }
        for hit in es_scroll(
            session,
            base,
            auth,
            args.log_index,
            body,
            args.timeout,
        ):
            source = hit.get("_source", {})
            match = SAVE_RE.search(str(source.get("message") or ""))
            timestamp = source.get("@timestamp")
            if match and timestamp:
                yield {
                    "save_timestamp": iso_z(
                        parse_datetime(str(timestamp))
                    ),
                    "png_filename": match.group("filename"),
                }

    expanded_start = start - timedelta(seconds=10)
    expanded_end = end + timedelta(seconds=10)
    parts_dir = path.parent / f"{path.stem}.parts"
    parts: list[Path] = []
    for chunk_start, chunk_end in iter_time_chunks(
        expanded_start,
        expanded_end,
        args.es_slice_hours,
    ):
        part = chunk_path(parts_dir, chunk_start, chunk_end)
        parts.append(part)
        if part.is_file():
            continue
        print(
            f"    save slice {iso_z(chunk_start)} -> "
            f"{iso_z(chunk_end)}",
            flush=True,
        )
        write_csv_atomic(
            part,
            SAVE_FIELDS,
            rows(chunk_start, chunk_end),
        )
    return write_csv_atomic(
        path,
        SAVE_FIELDS,
        iter_csv_rows(parts),
    )


def nearest_index(
    timestamps: list[datetime],
    target: datetime,
    max_delta_seconds: float,
) -> int | None:
    if not timestamps:
        return None
    position = bisect_left(timestamps, target)
    candidates = [
        index
        for index in (position - 1, position)
        if 0 <= index < len(timestamps)
    ]
    chosen = min(
        candidates,
        key=lambda index: abs(timestamps[index] - target),
    )
    if (
        abs(timestamps[chosen] - target).total_seconds()
        > max_delta_seconds
    ):
        return None
    return chosen


def build_day_candidates(
    predictions_path: Path,
    syncs_path: Path,
    saves_path: Path,
    output_path: Path,
    args: argparse.Namespace,
) -> int:
    if output_path.is_file():
        return sum(1 for _ in output_path.open(encoding="utf-8")) - 1

    syncs_by_tote: dict[str, list[dict[str, str]]] = {}
    for row in read_csv(syncs_path):
        syncs_by_tote.setdefault(row["tote_id"], []).append(row)
    sync_times_by_tote: dict[str, list[datetime]] = {}
    for tote, rows in syncs_by_tote.items():
        rows.sort(key=lambda row: row["latest_image_timestamp"])
        sync_times_by_tote[tote] = [
            parse_datetime(row["latest_image_timestamp"]) for row in rows
        ]

    saves = read_csv(saves_path)
    saves.sort(key=lambda row: row["save_timestamp"])
    save_times = [
        parse_datetime(row["save_timestamp"]) for row in saves
    ]

    def rows():
        seen: set[tuple[str, str]] = set()
        for prediction in read_csv(predictions_path):
            image_time = parse_datetime(prediction["image_msg_timestamp"])
            tote = prediction["tote_id"]
            tote_syncs = syncs_by_tote.get(tote, [])
            sync_index = nearest_index(
                sync_times_by_tote.get(tote, []),
                image_time,
                args.sync_delta_seconds,
            )
            if sync_index is None:
                continue
            sync = tote_syncs[sync_index]
            save_index = nearest_index(
                save_times,
                image_time,
                args.save_delta_seconds,
            )
            if save_index is None:
                continue
            save = saves[save_index]
            uri = (
                f"{args.s3_prefix.rstrip('/')}/"
                f"{save['png_filename'].lstrip('/')}"
            )
            key = (prediction["sku_id"], uri)
            if key in seen:
                continue
            seen.add(key)
            yield {
                "sku_id": prediction["sku_id"],
                "tote_id": tote,
                "prediction_timestamp": prediction["date_created"],
                "image_msg_timestamp": prediction["image_msg_timestamp"],
                "sync_timestamp": sync["sync_timestamp"],
                "latest_image_timestamp": sync[
                    "latest_image_timestamp"
                ],
                "save_timestamp": save["save_timestamp"],
                "png_filename": save["png_filename"],
                "s3_uri": uri,
            }

    return write_csv_atomic(output_path, CANDIDATE_FIELDS, rows())


def load_head_cache(path: Path) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    if not path.is_file():
        return result
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                row = json.loads(line)
                result[str(row["s3_uri"])] = row
    return result


def parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise ValueError(f"Invalid S3 URI: {uri}")
    return parsed.netloc, parsed.path.lstrip("/")


def head_one(s3, uri: str) -> dict[str, object]:
    bucket, key = parse_s3_uri(uri)
    try:
        response = s3.head_object(Bucket=bucket, Key=key)
        size = int(response["ContentLength"])
        return {
            "s3_uri": uri,
            "status": "valid" if size > 0 else "zero_byte",
            "s3_size_bytes": size,
            "s3_etag": str(response.get("ETag") or "").strip('"'),
        }
    except Exception as exc:
        return {
            "s3_uri": uri,
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        }


def select_valid_images(
    candidates: list[dict[str, str]],
    unique_skus: list[str],
    work_dir: Path,
    args: argparse.Namespace,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    candidates_by_sku: dict[str, list[dict[str, str]]] = {}
    uri_skus: dict[str, set[str]] = {}
    for row in candidates:
        sku = row["sku_id"]
        uri = row["s3_uri"]
        candidates_by_sku.setdefault(sku, []).append(row)
        uri_skus.setdefault(uri, set()).add(sku)
    for sku, rows in candidates_by_sku.items():
        rows.sort(
            key=lambda row: (
                len(uri_skus[row["s3_uri"]]),
                row["prediction_timestamp"],
                row["s3_uri"],
            )
        )

    cache_path = work_dir / "s3-head-results.jsonl"
    cache = load_head_cache(cache_path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_lock = threading.Lock()
    s3 = boto3.client(
        "s3",
        config=Config(
            retries={"max_attempts": 10, "mode": "adaptive"}
        ),
    )
    pending = set(unique_skus)
    positions = {sku: 0 for sku in unique_skus}
    selected: dict[str, dict[str, str]] = {}
    used_uris: set[str] = set()

    while pending:
        attempts: dict[str, list[str]] = {}
        exhausted: set[str] = set()
        for sku in sorted(pending):
            rows = candidates_by_sku.get(sku, [])
            position = positions[sku]
            while (
                position < len(rows)
                and rows[position]["s3_uri"] in used_uris
            ):
                position += 1
            positions[sku] = position
            if position >= len(rows):
                exhausted.add(sku)
                continue
            uri = rows[position]["s3_uri"]
            attempts.setdefault(uri, []).append(sku)

        pending -= exhausted
        if not attempts:
            break

        missing = [uri for uri in attempts if uri not in cache]
        if missing:
            print(
                f"Validating {len(missing)} S3 candidates for "
                f"{len(pending)} pending SKUs...",
                flush=True,
            )
            with cache_path.open("a", encoding="utf-8") as cache_stream:
                with ThreadPoolExecutor(
                    max_workers=args.s3_workers
                ) as executor:
                    future_to_uri = {
                        executor.submit(head_one, s3, uri): uri
                        for uri in missing
                    }
                    for future in as_completed(future_to_uri):
                        result = future.result()
                        uri = str(result["s3_uri"])
                        cache[uri] = result
                        with cache_lock:
                            cache_stream.write(
                                json.dumps(result, sort_keys=True) + "\n"
                            )
                            cache_stream.flush()

        progress = False
        for uri, skus in attempts.items():
            result = cache[uri]
            if result.get("status") == "valid" and uri not in used_uris:
                chosen_sku = min(
                    skus,
                    key=lambda sku: len(
                        candidates_by_sku.get(sku, [])
                    ),
                )
                candidate = candidates_by_sku[chosen_sku][
                    positions[chosen_sku]
                ]
                selected[chosen_sku] = {
                    **candidate,
                    "s3_size_bytes": str(
                        result["s3_size_bytes"]
                    ),
                    "s3_etag": str(result.get("s3_etag") or ""),
                    "selection_reason": (
                        "validated_unique_s3_path"
                        if len(uri_skus[uri]) == 1
                        else "validated_shared_candidate"
                    ),
                }
                used_uris.add(uri)
                pending.remove(chosen_sku)
                progress = True
            for sku in skus:
                if sku in pending:
                    positions[sku] += 1
                    progress = True
        if not progress:
            break

    selected_rows = [selected[sku] for sku in sorted(selected)]
    unmatched = [
        {
            "sku_id": sku,
            "candidate_count": str(
                len(candidates_by_sku.get(sku, []))
            ),
            "reason": (
                "no_log_mapped_candidate"
                if not candidates_by_sku.get(sku)
                else "no_distinct_nonzero_s3_candidate"
            ),
        }
        for sku in unique_skus
        if sku not in selected
    ]
    return selected_rows, unmatched


def write_outputs(
    output_dir: Path,
    selected: list[dict[str, str]],
    unmatched: list[dict[str, str]],
    unique_skus: list[str],
    summary: dict[str, object],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv_atomic(
        output_dir / "one-image-per-sku.csv",
        OUTPUT_FIELDS,
        selected,
    )
    write_csv_atomic(
        output_dir / "unmatched-skus.csv",
        ("sku_id", "candidate_count", "reason"),
        unmatched,
    )
    (output_dir / "unique-skus.txt").write_text(
        "".join(f"{sku}\n" for sku in unique_skus),
        encoding="utf-8",
    )
    with (output_dir / "s3-paths.txt").open(
        "w",
        encoding="utf-8",
    ) as stream:
        for row in selected:
            stream.write(f"{row['s3_uri']}\n")
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to")
    parser.add_argument("--start-utc")
    parser.add_argument("--end-utc")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--mongo-collection",
        default=DEFAULT_COLLECTION,
    )
    parser.add_argument("--es-secret", default=DEFAULT_ES_SECRET)
    parser.add_argument("--log-index", default=DEFAULT_LOG_INDEX)
    parser.add_argument("--s3-prefix", default=DEFAULT_S3_PREFIX)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--es-page-size", type=int, default=5000)
    parser.add_argument("--es-slice-hours", type=float, default=6.0)
    parser.add_argument("--s3-workers", type=int, default=32)
    parser.add_argument("--sync-delta-seconds", type=float, default=0.1)
    parser.add_argument("--save-delta-seconds", type=float, default=3.0)
    parser.add_argument("--approved-fallback", action="store_true")
    parser.add_argument("--approved-write", action="store_true")
    args = parser.parse_args()
    if not args.end_utc and not args.date_to:
        parser.error("Provide --date-to or --end-utc")
    if args.es_slice_hours <= 0:
        parser.error("--es-slice-hours must be positive")
    return args


def main() -> int:
    args = parse_args()
    if not args.approved_fallback:
        raise PermissionError(
            "Atlas prediction statistics are a fallback SKU source; "
            "obtain explicit approval and pass --approved-fallback"
        )
    if not args.approved_write:
        raise PermissionError(
            "Refusing to create durable artifacts without --approved-write"
        )

    output_dir = args.output_dir.expanduser().resolve()
    ensure_not_temporary(output_dir)
    start, end = compute_window(args)
    work_dir = output_dir / ".work"
    work_dir.mkdir(parents=True, exist_ok=True)

    collection, atlas_target, atlas_ip = atlas_collection(args)
    base, auth = es_credentials(args)
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
    )

    all_candidates: list[dict[str, str]] = []
    unique_skus: set[str] = set()
    prediction_count = 0
    sync_count = 0
    save_count = 0
    candidate_count = 0

    for day, slice_start, slice_end in iter_local_slices(start, end):
        print(
            f"Processing {day.isoformat()} "
            f"({iso_z(slice_start)} -> {iso_z(slice_end)})...",
            flush=True,
        )
        day_dir = work_dir / day.isoformat()
        predictions_path = day_dir / "predictions.csv"
        syncs_path = day_dir / "syncs.csv"
        saves_path = day_dir / "saves.csv"
        candidates_path = day_dir / "candidates.csv"

        day_predictions = export_predictions(
            collection,
            predictions_path,
            slice_start,
            slice_end,
        )
        day_syncs = export_syncs(
            session,
            base,
            auth,
            args,
            syncs_path,
            slice_start,
            slice_end,
        )
        day_saves = export_saves(
            session,
            base,
            auth,
            args,
            saves_path,
            slice_start,
            slice_end,
        )
        day_candidates = build_day_candidates(
            predictions_path,
            syncs_path,
            saves_path,
            candidates_path,
            args,
        )
        predictions = read_csv(predictions_path)
        unique_skus.update(
            row["sku_id"] for row in predictions if row["sku_id"]
        )
        all_candidates.extend(read_csv(candidates_path))
        prediction_count += day_predictions
        sync_count += day_syncs
        save_count += day_saves
        candidate_count += day_candidates
        print(
            f"  predictions={day_predictions}, syncs={day_syncs}, "
            f"saves={day_saves}, mapped_candidates={day_candidates}",
            flush=True,
        )

    sorted_skus = sorted(unique_skus)
    selected, unmatched = select_valid_images(
        all_candidates,
        sorted_skus,
        work_dir,
        args,
    )
    total_bytes = sum(int(row["s3_size_bytes"]) for row in selected)
    summary = {
        "source_type": "RobotEligibilityPredictionStats",
        "fallback_used": True,
        "metric_source_attempted_first": True,
        "metric_records_for_station_res1": 0,
        "fallback_approval_recorded": True,
        "window_start_utc": iso_z(start),
        "window_end_utc_exclusive": iso_z(end),
        "atlas_private_link_target": atlas_target,
        "atlas_reachable_ip": atlas_ip,
        "elasticsearch_endpoint": base,
        "elasticsearch_index": args.log_index,
        "prediction_records": prediction_count,
        "sync_records": sync_count,
        "save_records": save_count,
        "mapped_candidate_records": candidate_count,
        "unique_skus": len(sorted_skus),
        "mapped_skus": len(selected),
        "unmatched_skus": len(unmatched),
        "total_bytes": total_bytes,
        "total_gb": round(total_bytes / 1_000_000_000, 3),
        "total_gib": round(total_bytes / 1024**3, 3),
        "output_dir": str(output_dir),
        "mapping_csv": str(output_dir / "one-image-per-sku.csv"),
        "manifest": str(output_dir / "s3-paths.txt"),
        "images_downloaded": False,
    }
    write_outputs(
        output_dir,
        selected,
        unmatched,
        sorted_skus,
        summary,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2)
