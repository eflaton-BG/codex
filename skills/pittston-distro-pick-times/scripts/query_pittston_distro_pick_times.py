#!/usr/bin/env python3
"""Generate Pittston distro pick-time CSV data for one New York calendar date."""

from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

import requests


CLUSTER = "elastic-washington-cluster"
ALIAS = "pit-washington-metric_events-alias"
SITE_TIMEZONE = ZoneInfo("America/New_York")
DISTRO_EVENT_TYPES = ("DistroComplete", "DistroCompleteMessage")
PICK_EVENT_TYPE = "PickComplete"
COMPOSITE_PAGE_SIZE = 1000
TERMS_BATCH_SIZE = 5000


def parse_report_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD format") from exc


def utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def local_iso_from_epoch_ms(value: int | None) -> str:
    if value is None:
        return ""
    return datetime.fromtimestamp(value / 1000, timezone.utc).astimezone(SITE_TIMEZONE).isoformat(
        timespec="milliseconds"
    )


def format_duration(milliseconds: int) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def date_windows(report_date: date) -> dict[str, datetime]:
    distro_start_local = datetime.combine(report_date, time.min, SITE_TIMEZONE)
    distro_end_local = datetime.combine(report_date + timedelta(days=1), time.min, SITE_TIMEZONE)
    pick_start_local = datetime.combine(report_date - timedelta(days=4), time.min, SITE_TIMEZONE)
    return {
        "distro_start_local": distro_start_local,
        "distro_end_local": distro_end_local,
        "pick_start_local": pick_start_local,
        "pick_end_local": distro_end_local,
        "distro_start_utc": distro_start_local.astimezone(timezone.utc),
        "distro_end_utc": distro_end_local.astimezone(timezone.utc),
        "pick_start_utc": pick_start_local.astimezone(timezone.utc),
        "pick_end_utc": distro_end_local.astimezone(timezone.utc),
    }


def post_search(
    base_url: str,
    auth: tuple[str, str],
    body: dict[str, Any],
    timeout: int,
) -> dict[str, Any]:
    response = requests.post(
        f"{base_url}/{ALIAS}/_search",
        auth=auth,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        json=body,
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def composite_pages(
    base_url: str,
    auth: tuple[str, str],
    query: dict[str, Any],
    sub_aggs: dict[str, Any],
    timeout: int,
) -> Iterator[tuple[int | None, list[dict[str, Any]]]]:
    after_key: dict[str, Any] | None = None
    first_page = True

    while True:
        composite: dict[str, Any] = {
            "size": COMPOSITE_PAGE_SIZE,
            "sources": [{"distro_number": {"terms": {"field": "DistroNumber"}}}],
        }
        if after_key is not None:
            composite["after"] = after_key

        result = post_search(
            base_url,
            auth,
            {
                "size": 0,
                "track_total_hits": True,
                "query": query,
                "aggs": {
                    "distros": {
                        "composite": composite,
                        "aggs": sub_aggs,
                    }
                },
            },
            timeout,
        )
        total = result["hits"]["total"]
        total_value = total["value"] if isinstance(total, dict) else total
        aggregation = result["aggregations"]["distros"]
        buckets = aggregation.get("buckets", [])
        yield (int(total_value) if first_page else None), buckets
        first_page = False

        next_after_key = aggregation.get("after_key")
        if not buckets or not next_after_key or next_after_key == after_key:
            break
        after_key = next_after_key


def batched(values: list[int], size: int) -> Iterator[list[int]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def output_path_for(args: argparse.Namespace) -> Path:
    if args.output:
        return args.output.expanduser().resolve()
    caller_cwd = Path(os.environ.get("BG_ELASTIC_CALLER_CWD", Path.cwd()))
    return (caller_cwd / f"pittston_distro_pick_times_{args.date.isoformat()}.csv").resolve()


def query_report(args: argparse.Namespace) -> dict[str, Any]:
    windows = date_windows(args.date)
    output_path = output_path_for(args)

    resolved = {
        "cluster": CLUSTER,
        "alias": ALIAS,
        "site_timezone": str(SITE_TIMEZONE),
        "report_date": args.date.isoformat(),
        "distro_window_local": {
            "gte": windows["distro_start_local"].isoformat(),
            "lt": windows["distro_end_local"].isoformat(),
        },
        "distro_window_utc": {
            "gte": utc_iso(windows["distro_start_utc"]),
            "lt": utc_iso(windows["distro_end_utc"]),
        },
        "pick_window_local": {
            "gte": windows["pick_start_local"].isoformat(),
            "lt": windows["pick_end_local"].isoformat(),
        },
        "pick_window_utc": {
            "gte": utc_iso(windows["pick_start_utc"]),
            "lt": utc_iso(windows["pick_end_utc"]),
        },
        "output": str(output_path),
    }
    if args.dry_run:
        return {"dry_run": True, **resolved}

    from bg_vault_elastic.client import VaultElasticClient

    credentials = VaultElasticClient().get_es_credentials(CLUSTER)
    base_url = credentials["url"].rstrip("/")
    if ".es." not in base_url:
        raise RuntimeError(f"Vault returned a non-Elasticsearch endpoint: {base_url}")
    auth = (credentials["username"], credentials["password"])

    distro_query = {
        "bool": {
            "filter": [
                {
                    "range": {
                        "@timestamp": {
                            "gte": resolved["distro_window_utc"]["gte"],
                            "lt": resolved["distro_window_utc"]["lt"],
                        }
                    }
                },
                {"terms": {"EventType.keyword": list(DISTRO_EVENT_TYPES)}},
            ]
        }
    }

    distro_numbers: set[int] = set()
    distro_complete_documents = 0
    for total, buckets in composite_pages(base_url, auth, distro_query, {}, args.timeout):
        if total is not None:
            distro_complete_documents = total
        distro_numbers.update(int(bucket["key"]["distro_number"]) for bucket in buckets)

    sorted_distros = sorted(distro_numbers)
    pick_stats: dict[int, dict[str, Any]] = {}
    pick_complete_documents = 0
    pick_sub_aggs = {
        "first_pick": {"min": {"field": "@timestamp", "format": "strict_date_time"}},
        "last_pick": {"max": {"field": "@timestamp", "format": "strict_date_time"}},
        "pick_count": {"value_count": {"field": "@timestamp"}},
    }

    for distro_batch in batched(sorted_distros, TERMS_BATCH_SIZE):
        pick_query = {
            "bool": {
                "filter": [
                    {
                        "range": {
                            "@timestamp": {
                                "gte": resolved["pick_window_utc"]["gte"],
                                "lt": resolved["pick_window_utc"]["lt"],
                            }
                        }
                    },
                    {"term": {"EventType.keyword": PICK_EVENT_TYPE}},
                    {"terms": {"DistroNumber": distro_batch}},
                ]
            }
        }
        for total, buckets in composite_pages(
            base_url,
            auth,
            pick_query,
            pick_sub_aggs,
            args.timeout,
        ):
            if total is not None:
                pick_complete_documents += total
            for bucket in buckets:
                distro_number = int(bucket["key"]["distro_number"])
                first_value = bucket["first_pick"].get("value")
                last_value = bucket["last_pick"].get("value")
                first_ms = int(round(first_value)) if first_value is not None else None
                last_ms = int(round(last_value)) if last_value is not None else None
                pick_stats[distro_number] = {
                    "first_pick_utc": bucket["first_pick"].get("value_as_string", ""),
                    "last_pick_utc": bucket["last_pick"].get("value_as_string", ""),
                    "first_pick_ms": first_ms,
                    "last_pick_ms": last_ms,
                    "pick_count": int(bucket["pick_count"]["value"]),
                }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
    fieldnames = [
        "DistroNumber",
        "FirstPickUTC",
        "LastPickUTC",
        "FirstPickNewYork",
        "LastPickNewYork",
        "TotalPickTime",
        "TotalPickTimeSeconds",
        "PickCompleteCount",
    ]
    with temporary_path.open("w", newline="", encoding="utf-8") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=fieldnames)
        writer.writeheader()
        for distro_number in sorted_distros:
            stats = pick_stats.get(distro_number)
            first_ms = stats["first_pick_ms"] if stats else None
            last_ms = stats["last_pick_ms"] if stats else None
            duration_ms = last_ms - first_ms if first_ms is not None and last_ms is not None else None
            writer.writerow(
                {
                    "DistroNumber": distro_number,
                    "FirstPickUTC": stats["first_pick_utc"] if stats else "",
                    "LastPickUTC": stats["last_pick_utc"] if stats else "",
                    "FirstPickNewYork": local_iso_from_epoch_ms(first_ms),
                    "LastPickNewYork": local_iso_from_epoch_ms(last_ms),
                    "TotalPickTime": format_duration(duration_ms) if duration_ms is not None else "",
                    "TotalPickTimeSeconds": f"{duration_ms / 1000:.3f}"
                    if duration_ms is not None
                    else "",
                    "PickCompleteCount": stats["pick_count"] if stats else 0,
                }
            )
    temporary_path.replace(output_path)

    return {
        **resolved,
        "endpoint": base_url,
        "distro_complete_documents": distro_complete_documents,
        "unique_distro_numbers": len(sorted_distros),
        "pick_complete_documents_for_distros": pick_complete_documents,
        "distros_with_picks": len(pick_stats),
        "distros_without_picks": len(distro_numbers - pick_stats.keys()),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, type=parse_report_date, help="New York report date")
    parser.add_argument("--output", type=Path, help="Output CSV path")
    parser.add_argument("--timeout", type=int, default=180, help="Elasticsearch request timeout")
    parser.add_argument("--dry-run", action="store_true", help="Show date windows without querying")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print(json.dumps(query_report(args), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
