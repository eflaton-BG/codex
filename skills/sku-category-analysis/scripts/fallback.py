#!/usr/bin/env python3
"""Approved RES1 save-log fallback; preserves the metric-derived SKU inventory."""
import argparse
import hashlib
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from threading import Event

import prepare


def connection_id():
    connection = next(
        entry for entry in prepare.cli("list")
        if entry.get("connectionRef") == "elastic:washington"
    )
    identifier = connection["connectionId"]
    permissions = prepare.cli("permissions", identifier)
    enabled = [
        child for group in permissions.get("permissions", [])
        for child in group.get("children", []) if child.get("enabled")
    ]
    if not permissions.get("callable") or not any(
        child.get("path") == "/{index}/_search" for child in enabled
    ):
        raise RuntimeError("Washington index-search permission unavailable")
    return identifier


def read_log_slice(identifier, work, start, end, kind, exporter, stop_requested=None):
    """Split oversized/partial slices; never silently truncate log evidence."""
    if stop_requested is not None and stop_requested.is_set():
        raise RuntimeError("Stopped after another worker failed")
    body = {
        "size": 1000,
        "timeout": "20s",
        "track_total_hits": True,
        "_source": (
            ["@timestamp", "tote_id", "DonorToteId", "ToteID", "latest_image_timestamp"]
            if kind == "sync"
            else ["@timestamp", "message"]
        ),
        "query": {"bool": {"filter": [
            {"range": {"@timestamp": {"gte": prepare.stamp(start), "lt": prepare.stamp(end)}}},
            {"term": {"system_name.keyword": "res1"}},
            {"term": {"logger.keyword": (
                exporter.SYNC_LOGGER if kind == "sync" else exporter.SAVE_LOGGER
            )}},
        ]}},
    }
    if kind == "save":
        body["query"]["bool"]["must"] = [
            {"match_phrase": {"message": "Saved image topic /pick_scanner/rgb_camera/raw/image as"}}
        ]
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    path = work / f"{kind}-{digest}.json"
    if path.exists():
        payload = json.loads(path.read_text())
    else:
        # Failed HTTP responses must not become resumable complete artifacts.
        pending = path.with_suffix(".pending")
        response = None
        for attempt in range(2):
            try:
                response = prepare.cli(
                    "download", identifier, "--method", "POST",
                    "--path", "/pit-washington-log.records*/_search",
                    "--header", "Content-Type: application/json",
                    "--body-text", json.dumps(body), "--output", str(pending),
                    "--overwrite",
                )
                if response.get("status") == 200:
                    break
            except Exception:
                if attempt:
                    raise
        if not response or response.get("status") != 200:
            raise RuntimeError("Log slice query failed; pending response retained")
        payload = json.loads(pending.read_text())
    if "error" in payload:
        raise RuntimeError("Elasticsearch log slice returned an error")
    total = payload["hits"]["total"]
    complete = (
        not payload.get("timed_out")
        and not payload.get("_shards", {}).get("failed")
        and total["relation"] == "eq"
        and total["value"] <= body["size"]
        and total["value"] == len(payload["hits"]["hits"])
    )
    if not complete:
        if end - start <= timedelta(seconds=1):
            raise RuntimeError("Log slice incomplete even at one-second resolution")
        middle = start + (end - start) / 2
        return (
            read_log_slice(identifier, work, start, middle, kind, exporter, stop_requested)
            + read_log_slice(identifier, work, middle, end, kind, exporter, stop_requested)
        )
    if not path.exists():
        # Cache only fully validated exact hit sets.
        pending.replace(path)
    rows = []
    for hit in payload["hits"]["hits"]:
        source = hit["_source"]
        timestamp = source.get("@timestamp")
        if not timestamp:
            continue
        if kind == "sync":
            tote = next(
                (str(source[field]).strip() for field in ("tote_id", "DonorToteId", "ToteID")
                 if source.get(field)), "",
            )
            latest = source.get("latest_image_timestamp")
            if tote and latest:
                rows.append({
                    "sync_timestamp": exporter.iso_z(exporter.parse_datetime(timestamp)),
                    "tote_id": tote,
                    "latest_image_timestamp": exporter.iso_z(exporter.parse_datetime(str(latest))),
                })
        else:
            match = exporter.SAVE_RE.search(str(source.get("message") or ""))
            if match:
                rows.append({
                    "save_timestamp": exporter.iso_z(exporter.parse_datetime(timestamp)),
                    "png_filename": match.group("filename"),
                })
    return rows


def day_candidates(start, end, args, root, exporter, collection, identifier, skus, signature,
                   stop_requested=None):
    directory = root / ".fallback-work" / start.strftime("%Y%m%dT%H%M%SZ")
    directory.mkdir(parents=True, exist_ok=True)
    cached_path = directory / "complete.json"
    if cached_path.exists():
        cached = json.loads(cached_path.read_text())
        if cached["signature"] != signature:
            raise ValueError("Fallback cache configuration differs")
        return cached
    predictions = directory / "predictions.csv"
    prepare.save(directory / "status.json", {"phase": "predictions", "day": prepare.stamp(start)})
    exporter.export_predictions(collection, predictions, start, end)
    # Keep only the already verified metric inventory, never Atlas-only SKUs.
    filtered = directory / "metric-inventory-predictions.csv"
    prediction_rows = [
        row for row in exporter.read_csv(predictions) if row["sku_id"] in skus
    ]
    prepare.write_csv(filtered, list(exporter.PREDICTION_FIELDS), prediction_rows)
    paths = {}
    counts = {}
    for kind, fields in (("sync", exporter.SYNC_FIELDS), ("save", exporter.SAVE_FIELDS)):
        rows = []
        # Padding preserves matches whose logs arrive across day boundaries.
        first, last = start - timedelta(seconds=10), end + timedelta(seconds=10)
        while first < last:
            if stop_requested is not None and stop_requested.is_set():
                raise RuntimeError("Stopped after another worker failed")
            stop = min(last, first + timedelta(minutes=30))
            prepare.save(directory / "status.json", {
                "phase": kind, "slice_start": prepare.stamp(first),
                "slice_end": prepare.stamp(stop), "records_so_far": len(rows),
            })
            rows.extend(read_log_slice(identifier, directory, first, stop, kind, exporter,
                                       stop_requested))
            first = stop
        paths[kind] = directory / f"{kind}.csv"
        prepare.write_csv(paths[kind], list(fields), rows)
        counts[kind] = len(rows)
    raw = directory / "candidate-rows.csv"
    exporter.build_day_candidates(
        filtered, paths["sync"], paths["save"], raw,
        SimpleNamespace(
            s3_prefix=args.s3_prefix,
            sync_delta_seconds=args.sync_delta_seconds,
            save_delta_seconds=args.save_delta_seconds,
        ),
    )
    candidates = []
    for row in exporter.read_csv(raw):
        uri = prepare.image_uri(row["png_filename"], args.s3_prefix)
        if not uri:
            continue
        image = exporter.parse_datetime(row["image_msg_timestamp"])
        sync = exporter.parse_datetime(row["latest_image_timestamp"])
        save = exporter.parse_datetime(row["save_timestamp"])
        candidates.append({
            "SkuId": row["sku_id"], "prediction_timestamp": row["prediction_timestamp"],
            "image_msg_timestamp": row["image_msg_timestamp"],
            "image_timestamp": row["latest_image_timestamp"],
            "file_path": row["png_filename"], "s3_uri": uri,
            "topic": prepare.RGB_TOPIC, "tote_id": row["tote_id"],
            "sync_timestamp": row["sync_timestamp"], "save_timestamp": row["save_timestamp"],
            "sync_delta_seconds": abs((sync - image).total_seconds()),
            "save_delta_seconds": abs((save - image).total_seconds()),
            "mapping_method": "approved_tote_timestamp_save_log_fallback",
        })
    result = {
        "signature": signature, "day": prepare.stamp(start),
        "prediction_records": len(prediction_rows),
        "sync_records": counts["sync"], "rgb_save_records": counts["save"],
        "candidates": candidates,
    }
    prepare.save(cached_path, result)
    prepare.save(directory / "status.json", {"phase": "complete", "candidate_rows": len(candidates)})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to", required=True)
    parser.add_argument("--job-dir", required=True, type=Path)
    parser.add_argument("--approved-fallback", action="store_true")
    parser.add_argument("--approved-write", action="store_true")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--sync-delta-seconds", type=float, default=0.1)
    parser.add_argument("--save-delta-seconds", type=float, default=3.0)
    parser.add_argument("--s3-prefix", default="s3://washington-data/pittston/res1/image_data")
    args = parser.parse_args()
    if not args.approved_fallback or not args.approved_write:
        parser.error("Requires --approved-fallback and --approved-write")
    if not 1 <= args.workers <= 4:
        parser.error("Workers must be 1–4")
    if args.sync_delta_seconds < 0 or args.save_delta_seconds < 0:
        parser.error("Timestamp tolerances must be non-negative")
    start, end = prepare.bounds(args.date_from, args.date_to)
    root = args.job_dir.expanduser().resolve() / "manifest"
    skus = prepare.read_inventory(root, args)
    signature = hashlib.sha256(json.dumps({
        "skus": sorted(skus), "start": prepare.stamp(start), "end": prepare.stamp(end),
        "prefix": args.s3_prefix, "sync_delta": args.sync_delta_seconds,
        "save_delta": args.save_delta_seconds, "version": 1,
    }, sort_keys=True).encode()).hexdigest()
    work = root / ".fallback-work"
    work.mkdir(exist_ok=True)
    configuration = work / "configuration.json"
    if configuration.exists():
        if json.loads(configuration.read_text())["signature"] != signature:
            raise ValueError("Fallback work directory belongs to another inventory/configuration")
    else:
        prepare.save(configuration, {"signature": signature})
    spec = importlib.util.spec_from_file_location(
        "exporter", prepare.SKILL / "scripts/export_res1_sku_images.py",
    )
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)
    collection, _, _ = exporter.atlas_collection(
        SimpleNamespace(mongo_collection="robot_eligibility_prediction_stats")
    )
    identifier = connection_id()
    windows = []
    while start < end:
        stop = min(end, start + timedelta(days=1))
        windows.append((start, stop))
        start = stop
    candidates, counts = [], []
    stop_requested = Event()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(day_candidates, first, last, args, root, exporter,
                        collection, identifier, skus, signature, stop_requested): first
            for first, last in windows
        }
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as error:
                stop_requested.set()
                failed_day = futures[future].strftime("%Y%m%dT%H%M%SZ")
                # The query arguments contain no secrets; avoid serializing
                # arbitrary exception messages from credential clients.
                prepare.save(work / "last-error.json", {
                    "day": failed_day, "error_type": type(error).__name__,
                    "provider_stderr": getattr(error, "stderr", None),
                })
                for queued in futures:
                    queued.cancel()
                raise
            candidates.extend(result["candidates"])
            counts.append({key: value for key, value in result.items() if key not in ("signature", "candidates")})
            prepare.emit({**counts[-1], "candidate_rows": len(result["candidates"])})
    selected, missing = prepare.select_candidates(skus, candidates)
    fields = ["SkuId", "prediction_timestamp", "image_msg_timestamp", "image_timestamp",
              "file_path", "s3_uri", "topic", "tote_id", "sync_timestamp", "save_timestamp",
              "sync_delta_seconds", "save_delta_seconds", "mapping_method", "shared_image"]
    prepare.write_csv(root / "fallback-one-image-per-sku.csv", fields, selected)
    for row in missing:
        row["reason"] = "no_approved_save_log_fallback_match"
    prepare.write_csv(root / "fallback-unmatched-skus.csv", ["SkuId", "reason"], missing)
    summary = {
        "date_from": args.date_from, "date_to": args.date_to,
        "unique_skus": len(skus), "mapped_skus": len(selected), "unmatched_skus": len(missing),
        "shared_image_skus": sum(row["shared_image"] for row in selected),
        "mapping_method": "approved_tote_timestamp_save_log_fallback",
        "sync_delta_seconds_limit": args.sync_delta_seconds,
        "save_delta_seconds_limit": args.save_delta_seconds,
        "s3_objects_validated": False, "openai_calls": 0, "image_downloads": 0,
        "daily_counts": sorted(counts, key=lambda row: row["day"]),
    }
    prepare.save(root / "fallback-mapping.summary.json", summary)
    prepare.emit(summary)


if __name__ == "__main__":
    main()
