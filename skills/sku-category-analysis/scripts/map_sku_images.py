#!/usr/bin/env python3
"""Plan or export Pittston SKU images using an approved PickComplete fallback."""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo


LOCAL_TZ = ZoneInfo("America/New_York")
METRICS_INDEX = "pit-washington-metric_events*"
DEFAULT_S3_PREFIX = "s3://washington-data/pittston/res1/image_data/"
DEFAULT_ELASTIC_HELPER = (
    Path.home()
    / ".codex/skills/bg-elasticsearch/scripts/run_bg_vault_elastic_python.sh"
)
DEFAULT_MAPPING_EXPORTER = (
    Path.home()
    / ".codex/skills/map-pittston-revs-images/scripts/export_pittston_revs_table.py"
)

PLAN_QUERY_CODE = r"""
from bg_vault_elastic.client import VaultElasticClient
import json
import requests
import sys

start_utc, end_utc = sys.argv[1:3]
creds = VaultElasticClient().get_es_credentials("elastic-washington-cluster")
base = creds["url"].rstrip("/")
body = {
    "size": 0,
    "track_total_hits": True,
    "query": {
        "bool": {
            "filter": [
                {"range": {"@timestamp": {"gte": start_utc, "lt": end_utc}}},
                {"term": {"EventType.keyword": "PickComplete"}},
                {"exists": {"field": "ProductId.keyword"}},
                {"exists": {"field": "DonorToteId.keyword"}},
            ]
        }
    },
    "aggs": {
        "unique_skus": {
            "cardinality": {
                "field": "ProductId.keyword",
                "precision_threshold": 40000,
            }
        },
        "unique_totes": {
            "cardinality": {
                "field": "DonorToteId.keyword",
                "precision_threshold": 40000,
            }
        },
    },
}
response = requests.post(
    f"{base}/pit-washington-metric_events*/_search",
    auth=(creds["username"], creds["password"]),
    headers={"Accept": "application/json", "Content-Type": "application/json"},
    json=body,
    timeout=60,
)
response.raise_for_status()
data = response.json()
print(
    json.dumps(
        {
            "endpoint": base,
            "index": "pit-washington-metric_events*",
            "utc_range": [start_utc, end_utc],
            "pickcomplete_events": data["hits"]["total"]["value"],
            "estimated_unique_skus": data["aggregations"]["unique_skus"]["value"],
            "estimated_unique_totes": data["aggregations"]["unique_totes"]["value"],
            "read_only": True,
        },
        indent=2,
    )
)
"""

MAPPING_FIELDS = {
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
}


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def validate_dates(date_from: date, date_to: date) -> None:
    if date_to < date_from:
        raise ValueError("--date-to must be on or after --date-from")


def utc_range(date_from: date, date_to: date) -> tuple[str, str]:
    local_start = datetime.combine(date_from, time.min, tzinfo=LOCAL_TZ)
    local_end = datetime.combine(
        date_to + timedelta(days=1), time.min, tzinfo=LOCAL_TZ
    )
    return (
        local_start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        local_end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    )


def run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=False)


def run_plan(args: argparse.Namespace) -> int:
    if not args.approved_fallback:
        raise PermissionError(
            "PickComplete is a non-metric SKU fallback. Ask the user for "
            "explicit approval, then pass --approved-fallback."
        )
    date_from = parse_date(args.date_from)
    date_to = parse_date(args.date_to)
    validate_dates(date_from, date_to)
    start_utc, end_utc = utc_range(date_from, date_to)
    helper = args.elastic_helper.expanduser().resolve()
    if not helper.is_file():
        raise FileNotFoundError(f"Elasticsearch helper does not exist: {helper}")

    command = [
        "/bin/bash",
        str(helper),
        "-c",
        PLAN_QUERY_CODE,
        start_utc,
        end_utc,
    ]
    result = run_command(command)
    if result.stdout:
        print(result.stdout, end="")
    if result.returncode:
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")
        return result.returncode
    return 0


def derived_path(mapping_output: Path, suffix: str) -> Path:
    return mapping_output.with_name(f"{mapping_output.stem}{suffix}")


def output_paths(args: argparse.Namespace) -> dict[str, Path]:
    mapping = args.mapping_output.expanduser().resolve()
    return {
        "mapping": mapping,
        "manifest": (
            args.manifest_output.expanduser().resolve()
            if args.manifest_output
            else derived_path(mapping, ".one-per-sku.csv")
        ),
        "s3": (
            args.s3_output.expanduser().resolve()
            if args.s3_output
            else derived_path(mapping, ".one-per-sku.s3.csv")
        ),
        "unmatched": (
            args.unmatched_output.expanduser().resolve()
            if args.unmatched_output
            else derived_path(mapping, ".unmatched-skus.csv")
        ),
        "work": (
            args.work_dir.expanduser().resolve()
            if args.work_dir
            else mapping.parent / f"{mapping.name}.work"
        ),
    }


def ensure_writable_plan(paths: dict[str, Path], overwrite: bool) -> None:
    existing = [str(path) for path in paths.values() if path.exists()]
    if existing and not overwrite:
        raise FileExistsError(
            "Refusing to overwrite existing paths without --overwrite: "
            + ", ".join(existing)
        )


def normalize_png_filename(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    marker = "/image_data/"
    if marker in normalized:
        normalized = normalized.split(marker, 1)[1]
    return normalized.lstrip("/")


def read_mapping(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        missing = MAPPING_FIELDS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"Mapping CSV is missing columns: {sorted(missing)}"
            )
        return [dict(row) for row in reader]


def choose_rows(
    rows: list[dict[str, str]],
    s3_prefix: str,
) -> tuple[list[dict[str, str]], list[str]]:
    all_skus = sorted(
        {
            row["product_id"].strip()
            for row in rows
            if row["product_id"].strip()
        }
    )
    candidates_by_sku: dict[str, list[dict[str, str]]] = defaultdict(list)
    png_to_skus: dict[str, set[str]] = defaultdict(set)

    for row in rows:
        sku = row["product_id"].strip()
        png = normalize_png_filename(row["png_filename"])
        if not sku or not png:
            continue
        candidate = dict(row)
        candidate["png_filename"] = png
        candidates_by_sku[sku].append(candidate)
        png_to_skus[png].add(sku)

    selected: list[dict[str, str]] = []
    unmatched: list[str] = []
    for sku in all_skus:
        candidates = sorted(
            candidates_by_sku.get(sku, []),
            key=lambda row: (
                row.get("pair_first_seen_ts", ""),
                row.get("date", ""),
                row.get("png_filename", ""),
            ),
        )
        if not candidates:
            unmatched.append(sku)
            continue

        unshared = [
            row
            for row in candidates
            if len(png_to_skus[row["png_filename"]]) == 1
        ]
        chosen = unshared[0] if unshared else candidates[0]
        shared_count = len(png_to_skus[chosen["png_filename"]])
        selected.append(
            {
                "product_id": sku,
                "s3_uri": (
                    f"{s3_prefix.rstrip('/')}/"
                    f"{chosen['png_filename'].lstrip('/')}"
                ),
                "png_filename": chosen["png_filename"],
                "date": chosen.get("date", ""),
                "donor_tote_id": chosen.get("donor_tote_id", ""),
                "pair_first_seen_ts": chosen.get("pair_first_seen_ts", ""),
                "sync_ts": chosen.get("sync_ts", ""),
                "latest_image_timestamp": chosen.get(
                    "latest_image_timestamp", ""
                ),
                "prediction_ts": chosen.get("prediction_ts", ""),
                "is_eligible": chosen.get("is_eligible", ""),
                "reason": chosen.get("reason", ""),
                "shared_png_sku_count": str(shared_count),
                "ambiguous_shared_png": str(shared_count > 1).lower(),
                "selection_reason": (
                    "unique_png_for_sku"
                    if shared_count == 1
                    else "shared_png_fallback"
                ),
            }
        )
    return selected, unmatched


def write_dict_rows(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0]) if rows else [
        "product_id",
        "s3_uri",
        "png_filename",
        "date",
        "donor_tote_id",
        "pair_first_seen_ts",
        "sync_ts",
        "latest_image_timestamp",
        "prediction_ts",
        "is_eligible",
        "reason",
        "shared_png_sku_count",
        "ambiguous_shared_png",
        "selection_reason",
    ]
    with path.open("w", newline="", encoding="utf-8") as destination:
        writer = csv.DictWriter(destination, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_s3_rows(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as destination:
        writer = csv.writer(destination)
        writer.writerows([[row["s3_uri"]] for row in rows])


def write_unmatched(path: Path, skus: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as destination:
        writer = csv.writer(destination)
        writer.writerow(["product_id"])
        writer.writerows([[sku] for sku in skus])


def run_export(args: argparse.Namespace) -> int:
    if not args.approved_fallback:
        raise PermissionError(
            "PickComplete is a non-metric SKU fallback. Ask the user for "
            "explicit approval, then pass --approved-fallback."
        )
    if not args.approved_write:
        raise PermissionError(
            "Refusing to write mapping artifacts without --approved-write"
        )

    date_from = parse_date(args.date_from)
    date_to = parse_date(args.date_to)
    validate_dates(date_from, date_to)
    paths = output_paths(args)
    ensure_writable_plan(paths, args.overwrite)

    helper = args.elastic_helper.expanduser().resolve()
    exporter = args.mapping_exporter.expanduser().resolve()
    if not helper.is_file():
        raise FileNotFoundError(f"Elasticsearch helper does not exist: {helper}")
    if not exporter.is_file():
        raise FileNotFoundError(f"Mapping exporter does not exist: {exporter}")

    command = [
        "/bin/bash",
        str(helper),
        str(exporter),
        "--date-from",
        date_from.isoformat(),
        "--date-to",
        date_to.isoformat(),
        "--output",
        str(paths["mapping"]),
        "--work-dir",
        str(paths["work"]),
    ]
    result = subprocess.run(command, check=False)
    if result.returncode:
        return result.returncode

    rows = read_mapping(paths["mapping"])
    selected, unmatched = choose_rows(rows, args.s3_prefix)
    write_dict_rows(paths["manifest"], selected)
    write_s3_rows(paths["s3"], selected)
    write_unmatched(paths["unmatched"], unmatched)
    ambiguous = sum(
        row["ambiguous_shared_png"] == "true" for row in selected
    )
    print(
        json.dumps(
            {
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
                "mapping_rows": len(rows),
                "mapped_skus": len(selected),
                "unmatched_skus": len(unmatched),
                "ambiguous_shared_png_skus": ambiguous,
                "outputs": {key: str(value) for key, value in paths.items()},
                "images_downloaded": False,
            },
            indent=2,
        )
    )
    return 0


def add_date_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--date-from", required=True, help="Inclusive local date.")
    parser.add_argument("--date-to", required=True, help="Inclusive local date.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser(
        "plan", help="Read-only count for a local date range."
    )
    add_date_arguments(plan_parser)
    plan_parser.add_argument(
        "--elastic-helper", type=Path, default=DEFAULT_ELASTIC_HELPER
    )
    plan_parser.add_argument(
        "--approved-fallback",
        action="store_true",
        help="Required after explicit approval to use PickComplete.",
    )

    export_parser = subparsers.add_parser(
        "export", help="Export mappings and one-image-per-SKU manifests."
    )
    add_date_arguments(export_parser)
    export_parser.add_argument("--mapping-output", type=Path, required=True)
    export_parser.add_argument("--manifest-output", type=Path)
    export_parser.add_argument("--s3-output", type=Path)
    export_parser.add_argument("--unmatched-output", type=Path)
    export_parser.add_argument("--work-dir", type=Path)
    export_parser.add_argument(
        "--s3-prefix", default=DEFAULT_S3_PREFIX
    )
    export_parser.add_argument(
        "--elastic-helper", type=Path, default=DEFAULT_ELASTIC_HELPER
    )
    export_parser.add_argument(
        "--mapping-exporter", type=Path, default=DEFAULT_MAPPING_EXPORTER
    )
    export_parser.add_argument("--overwrite", action="store_true")
    export_parser.add_argument(
        "--approved-write",
        action="store_true",
        help="Required only after explicit approval to write mapping artifacts.",
    )
    export_parser.add_argument(
        "--approved-fallback",
        action="store_true",
        help="Required after explicit approval to use PickComplete.",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "plan":
        return run_plan(args)
    if args.command == "export":
        return run_export(args)
    raise AssertionError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2)
