#!/usr/bin/env python3
"""Date-driven Pittston RES1 preparation. Never calls OpenAI or downloads images."""
import argparse
import csv
import hashlib
import importlib.util
import json
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

SKILL = Path.home() / ".codex/skills/frontier-image-annotation"
BGA = Path.home() / ".codex/skills/bga-connections/bga-connections.py"
RGB_TOPIC = "/pick_scanner/rgb_camera/raw/image"
UTC = timezone.utc


def bounds(first, last):
    first, last = date.fromisoformat(first), date.fromisoformat(last)
    if last < first:
        raise ValueError("End date precedes start date")
    zone = ZoneInfo("America/New_York")
    return (
        datetime.combine(first, time.min, zone).astimezone(UTC),
        datetime.combine(last + timedelta(days=1), time.min, zone).astimezone(UTC),
    )


def stamp(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def emit(value):
    print(json.dumps(value, default=str, sort_keys=True), flush=True)


def save(path, value):
    temp = path.with_suffix(path.suffix + ".partial")
    temp.write_text(json.dumps(value, default=str, indent=2) + "\n")
    temp.replace(path)


def cli(*arguments):
    result = subprocess.run(
        [sys.executable, str(BGA), *arguments],
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)


def validate_search(payload):
    if payload.get("timed_out") or payload.get("_shards", {}).get("failed"):
        raise RuntimeError("Incomplete Elasticsearch response")
    if "error" in payload:
        raise RuntimeError("Elasticsearch returned an error")


def write_csv(path, fields, rows):
    temp = path.with_suffix(path.suffix + ".partial")
    with temp.open("w", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    temp.replace(path)


def metric_export(args, start, end, root):
    connections = cli("list")
    connection = next(c for c in connections if c.get("connectionRef") == "elastic:washington")
    connection_id = connection["connectionId"]
    permissions = cli("permissions", connection_id)
    if not permissions.get("callable"):
        raise RuntimeError("Washington connection unavailable")
    enabled = [
        child for group in permissions.get("permissions", [])
        for child in group.get("children", []) if child.get("enabled")
    ]
    if not any(c.get("path") == "/{index}/_search" for c in enabled):
        raise RuntimeError("Index search permission unavailable")
    query = {"bool": {"filter": [
        {"range": {"@timestamp": {"gte": stamp(start), "lt": stamp(end)}}},
        {"term": {"EventType.keyword": "SkuRobotEligibilityChange"}},
        {"term": {"StationId.keyword": "RES1"}},
        {"term": {"Source.keyword": "RES"}},
        {"exists": {"field": "SkuId.keyword"}},
    ]}}
    skus, after, total, page = set(), None, None, 0
    while True:
        composite = {"size": 1000, "sources": [{"sku": {"terms": {"field": "SkuId.keyword"}}}]}
        if after:
            composite["after"] = after
        body = {"size": 0, "track_total_hits": True, "query": query,
                "aggs": {"unique_skus": {"composite": composite}}}
        destination = root / f"metrics-page-{page:04d}.json"
        request = cli(
            "download", connection_id, "--method", "POST",
            "--path", "/pit-washington-metric_events*/_search",
            "--header", "Content-Type: application/json",
            "--body-text", json.dumps(body), "--output", str(destination),
        )
        if request.get("status") != 200:
            raise RuntimeError("Metric query failed")
        payload = json.loads(destination.read_text())
        validate_search(payload)
        hits = payload["hits"]["total"]
        if hits["relation"] != "eq":
            raise RuntimeError("Metric count is not exact")
        if total is not None and total != hits["value"]:
            raise RuntimeError("Metric source changed during pagination")
        total = hits["value"]
        aggregation = payload["aggregations"]["unique_skus"]
        buckets = aggregation["buckets"]
        skus.update(str(b["key"]["sku"]).strip() for b in buckets if str(b["key"]["sku"]).strip())
        next_after = aggregation.get("after_key")
        emit({"phase": "metrics", "page": page, "unique_skus_so_far": len(skus)})
        if not buckets or not next_after:
            break
        if next_after == after:
            raise RuntimeError("Pagination cursor did not advance")
        after, page = next_after, page + 1
    if not skus:
        raise RuntimeError("No metric SKUs; fallback requires separate approval")
    write_csv(root / "unique-skus.csv", ["SkuId"], ({"SkuId": sku} for sku in sorted(skus)))
    save(root / "unique-skus.summary.json", {
        "source": "SkuRobotEligibilityChange", "station_id": "RES1",
        "date_from": args.date_from, "date_to": args.date_to,
        "timezone": "America/New_York", "start_utc": stamp(start),
        "end_utc_exclusive": stamp(end), "metric_records": total,
        "unique_skus": len(skus), "csv_rows_with_header": len(skus) + 1,
        "fallback_used": False, "openai_calls": 0,
    })


def image_uri(path, prefix):
    value = str(path or "").strip()
    if not value:
        return None
    parsed = PurePosixPath(value)
    if value.startswith("/") or ":" in value or ".." in parsed.parts:
        raise ValueError("Image path must be a safe relative key")
    if parsed.suffix.lower() != ".png":
        return None
    return prefix.rstrip("/") + "/" + str(parsed)


def read_inventory(root, args):
    summary = json.loads((root / "unique-skus.summary.json").read_text())
    if summary["date_from"] != args.date_from or summary["date_to"] != args.date_to:
        raise ValueError("SKU inventory date range does not match requested mapping")
    if summary.get("source", summary.get("event_type")) != "SkuRobotEligibilityChange":
        raise ValueError("Expected the metric-derived inventory")
    with (root / "unique-skus.csv").open() as source:
        rows = list(csv.DictReader(source))
    skus = {row["SkuId"] for row in rows}
    if "" in skus or len(rows) != len(skus) or len(skus) != summary["unique_skus"]:
        raise ValueError("Invalid inventory count, blank SKU, or duplicates")
    return skus


def select_candidates(skus, candidates):
    owners = defaultdict(set)
    by_sku = defaultdict(list)
    for row in candidates:
        if row["SkuId"] not in skus:
            raise ValueError("Mapping introduced a non-inventory SKU")
        owners[row["s3_uri"]].add(row["SkuId"])
        by_sku[row["SkuId"]].append(row)
    selected, missing = [], []
    for sku in sorted(skus):
        rows = by_sku.get(sku, [])
        if not rows:
            missing.append({"SkuId": sku, "reason": "no_native_rgb_image_match"})
            continue
        row = min(rows, key=lambda r: (
            len(owners[r["s3_uri"]]) > 1, r["prediction_timestamp"], r["s3_uri"],
        ))
        selected.append({**row, "shared_image": len(owners[row["s3_uri"]]) > 1})
    return selected, missing


def native_map(args, start, end, root):
    if not args.approved_native_read:
        raise PermissionError("Atlas reads require --approved-native-read")
    skus = read_inventory(root, args)
    spec = importlib.util.spec_from_file_location("res1_exporter", SKILL / "scripts/export_res1_sku_images.py")
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)
    predictions, _, _ = exporter.atlas_collection(
        SimpleNamespace(mongo_collection="robot_eligibility_prediction_stats")
    )
    client = predictions.database.client
    if args.image_database not in client.list_database_names():
        raise RuntimeError("Configured image database does not exist")
    database = client[args.image_database]
    if "image_data" not in database.list_collection_names():
        raise RuntimeError("Configured image collection does not exist")
    images = database["image_data"]
    if not any(list(index["key"])[0][0] in ("timestamp", "topic")
               for index in images.index_information().values()):
        raise RuntimeError("No timestamp/topic index; refusing repeated collection scans")
    signature = hashlib.sha256(json.dumps({
        "skus": sorted(skus), "start": stamp(start), "end": stamp(end),
        "database": args.image_database, "topic": RGB_TOPIC, "prefix": args.s3_prefix,
    }, sort_keys=True).encode()).hexdigest()
    work = root / ".native-work"
    work.mkdir(exist_ok=True)
    all_candidates = []
    day = start
    while day < end:
        stop = min(end, day + timedelta(days=1))
        chunk = work / (day.strftime("%Y%m%dT%H%M%SZ") + ".json")
        if chunk.exists():
            cached = json.loads(chunk.read_text())
            if cached["signature"] != signature:
                raise ValueError("Cached native mapping configuration differs")
        else:
            by_time = defaultdict(list)
            count = 0
            query = {"date_created": {"$gte": day, "$lt": stop}}
            projection = {"_id": 0, "sku_id": 1, "date_created": 1, "image_msg_timestamp": 1}
            for row in predictions.find(query, projection, batch_size=2000).max_time_ms(120000):
                if row.get("sku_id") not in skus:
                    continue
                count += 1
                timestamp = row.get("image_msg_timestamp")
                if isinstance(timestamp, datetime):
                    by_time[timestamp].append(row)
            candidates = []
            if by_time:
                # BSON dates have millisecond precision; equality is the native
                # [image_msg_timestamp, image_msg_timestamp + 1ms) join.
                query = {"topic": RGB_TOPIC, "timestamp": {
                    "$gte": min(by_time), "$lt": max(by_time) + timedelta(milliseconds=1),
                }}
                for image in images.find(
                    query, {"timestamp": 1, "file_path": 1, "topic": 1}, batch_size=2000,
                ).sort([("timestamp", 1), ("_id", 1)]).max_time_ms(120000):
                    records = by_time.get(image.get("timestamp"), [])
                    uri = image_uri(image.get("file_path"), args.s3_prefix)
                    if not records or not uri:
                        continue
                    for row in records:
                        candidates.append({
                            "SkuId": row["sku_id"], "prediction_timestamp": stamp(row["date_created"]),
                            "image_msg_timestamp": stamp(row["image_msg_timestamp"]),
                            "image_timestamp": stamp(image["timestamp"]),
                            "file_path": image["file_path"], "s3_uri": uri,
                            "topic": RGB_TOPIC,
                        })
                    # Select the earliest valid RGB ImageData record at this time.
                    del by_time[image["timestamp"]]
            cached = {"signature": signature, "prediction_records": count, "candidates": candidates}
            save(chunk, cached)
        all_candidates.extend(cached["candidates"])
        emit({"phase": "native-map", "day": stamp(day),
              "prediction_records": cached["prediction_records"],
              "candidate_rows": len(cached["candidates"])})
        day = stop
    selected, missing = select_candidates(skus, all_candidates)
    fields = ["SkuId", "prediction_timestamp", "image_msg_timestamp", "image_timestamp",
              "file_path", "s3_uri", "topic", "shared_image"]
    write_csv(root / "native-one-image-per-sku.csv", fields, selected)
    write_csv(root / "native-unmatched-skus.csv", ["SkuId", "reason"], missing)
    summary = {
        "unique_skus": len(skus), "mapped_skus": len(selected), "unmatched_skus": len(missing),
        "shared_image_skus": sum(row["shared_image"] for row in selected),
        "image_database": args.image_database, "topic": RGB_TOPIC,
        "s3_objects_validated": False, "openai_calls": 0, "image_downloads": 0,
        "date_from": args.date_from, "date_to": args.date_to,
    }
    save(root / "native-mapping.summary.json", summary)
    emit(summary)


def validate_s3(args, root):
    """HEAD-only verification using the user's host AWS credential chain."""
    if not args.approved_s3_read:
        raise PermissionError("S3 metadata reads require --approved-s3-read")
    import boto3
    from botocore.exceptions import ClientError

    skus = read_inventory(root, args)
    source_kind = getattr(args, "mapping_source", "native")
    summary = json.loads((root / f"{source_kind}-mapping.summary.json").read_text())
    if (summary["date_from"], summary["date_to"]) != (args.date_from, args.date_to):
        raise ValueError("Native mapping dates differ")
    with (root / f"{source_kind}-one-image-per-sku.csv").open() as source:
        reader = csv.DictReader(source)
        fields = list(reader.fieldnames)
        rows = list(reader)
    if not rows:
        raise RuntimeError("No native mappings to validate; fallback requires approval")
    mapped_skus = [row["SkuId"] for row in rows]
    if len(set(mapped_skus)) != len(mapped_skus) or not set(mapped_skus) <= skus:
        raise ValueError("Mapping contains duplicate or non-inventory SKUs")
    if "mapped_skus" in summary and len(rows) != summary["mapped_skus"]:
        raise ValueError("Mapping count does not match summary")
    session = boto3.Session()
    identity = session.client("sts").get_caller_identity()
    emit({"phase": "s3-validation", "aws_account": identity["Account"], "aws_arn": identity["Arn"]})
    client = session.client("s3")
    cache_dir = root / f".{source_kind}-work"
    cache_dir.mkdir(exist_ok=True)
    cache_path = cache_dir / "s3-head-cache.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    workers = getattr(args, "s3_workers", 8)
    if not 1 <= workers <= 16:
        raise ValueError("S3 worker count must be 1–16")
    unique_uris = sorted({row["s3_uri"] for row in rows})
    for uri in unique_uris:
        parsed = urlsplit(uri)
        if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.lstrip("/") or parsed.query or parsed.fragment:
            raise ValueError("Invalid S3 URI in mapping manifest")

    def head(uri):
        parsed = urlsplit(uri)
        try:
            response = client.head_object(Bucket=parsed.netloc, Key=parsed.path.lstrip("/"))
            result = {"s3_size_bytes": response["ContentLength"],
                      "s3_etag": response.get("ETag", "")}
        except ClientError as error:
            code = error.response["Error"]["Code"]
            if code not in ("404", "NoSuchKey", "NotFound"):
                raise
            result = {"s3_size_bytes": 0, "s3_etag": "", "reason": "object_missing"}
        result["checked_at"] = stamp(datetime.now(UTC))
        return result

    pending = [uri for uri in unique_uris if uri not in cache]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for offset in range(0, len(pending), 100):
            # Limit queued work so an authentication error stops promptly.
            futures = {pool.submit(head, uri): uri for uri in pending[offset:offset + 100]}
            try:
                for future in as_completed(futures):
                    cache[futures[future]] = future.result()
            except Exception:
                for future in futures:
                    future.cancel()
                raise
            finally:
                save(cache_path, cache)
            emit({"phase": "s3-validation", "checked_objects": min(offset + 100, len(pending)),
                  "pending_objects_at_start": len(pending)})

    validated, rejected = [], []
    for row in rows:
        uri = row["s3_uri"]
        result = cache[uri]
        if result["s3_size_bytes"] > 0:
            validated.append({**row, "s3_size_bytes": result["s3_size_bytes"],
                              "s3_etag": result["s3_etag"]})
        else:
            rejected.append({"SkuId": row["SkuId"], "s3_uri": uri,
                             "reason": result.get("reason", "object_empty")})
    save(cache_path, cache)
    write_csv(root / "one-image-per-sku.csv", fields + ["s3_size_bytes", "s3_etag"], validated)
    write_csv(root / "s3-rejected-skus.csv", ["SkuId", "s3_uri", "reason"], rejected)
    uris = sorted({row["s3_uri"] for row in validated})
    temp = root / "s3-paths.txt.partial"
    temp.write_text("".join(uri + "\n" for uri in uris))
    temp.replace(root / "s3-paths.txt")
    # Shared images contribute bytes only once.
    total = sum(cache[uri]["s3_size_bytes"] for uri in uris)
    summary.update({
        "s3_objects_validated": True, "validated_skus": len(validated),
        "rejected_skus": len(rejected), "unique_s3_objects": len(uris),
        "s3_total_bytes": total, "s3_total_gb": total / 1e9,
        "s3_total_gib": total / 1024**3,
        "aws_account": identity["Account"], "aws_arn": identity["Arn"],
        "validation_cache": str(cache_path),
        "mapping_source": source_kind,
    })
    save(root / "s3-validation.summary.json", summary)
    emit(summary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["metrics", "native-map", "validate-s3"])
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to", required=True, help="Inclusive Eastern local date")
    parser.add_argument("--job-dir", required=True, type=Path)
    parser.add_argument("--approved-write", action="store_true")
    parser.add_argument("--approved-native-read", action="store_true")
    parser.add_argument("--approved-s3-read", action="store_true")
    parser.add_argument("--mapping-source", choices=["native", "fallback"], default="native")
    parser.add_argument("--s3-workers", type=int, default=8)
    parser.add_argument("--image-database", default="washington_pit_washington_perception_data")
    parser.add_argument("--s3-prefix", default="s3://washington-data/pittston/res1/image_data")
    args = parser.parse_args()
    start, end = bounds(args.date_from, args.date_to)
    if not args.approved_write:
        parser.error("Artifact writes require --approved-write")
    if args.command == "native-map" and not args.approved_native_read:
        parser.error("Atlas reads require --approved-native-read")
    if args.command == "validate-s3" and not args.approved_s3_read:
        parser.error("S3 metadata reads require --approved-s3-read")
    root = args.job_dir.expanduser().resolve() / "manifest"
    root.mkdir(parents=True, exist_ok=True)
    if args.command == "metrics":
        if (root / "unique-skus.csv").exists():
            parser.error("Inventory exists; preserve it or select a new job directory")
        metric_export(args, start, end, root)
    elif args.command == "native-map":
        native_map(args, start, end, root)
    else:
        validate_s3(args, root)


if __name__ == "__main__":
    main()
