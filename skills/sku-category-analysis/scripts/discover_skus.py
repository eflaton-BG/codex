#!/usr/bin/env python3
"""Discover an exact unique SKU list from eligibility-change metrics."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import requests


DEFAULT_INDEX = "pit-washington-metric_events*"
DEFAULT_ELASTIC_SECRET = "elastic-washington-cluster"
DEFAULT_EVENT_TYPE = "SkuRobotEligibilityChange"


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def utc_range(
    date_from: date,
    date_to: date,
    timezone_name: str,
) -> tuple[str, str]:
    if date_to < date_from:
        raise ValueError("--date-to must be on or after --date-from")
    local_timezone = ZoneInfo(timezone_name)
    start = datetime.combine(date_from, time.min, tzinfo=local_timezone)
    end = datetime.combine(
        date_to + timedelta(days=1),
        time.min,
        tzinfo=local_timezone,
    )
    return (
        start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    )


def query_unique_skus(args: argparse.Namespace) -> dict[str, object]:
    from bg_vault_elastic.client import VaultElasticClient

    date_from = parse_date(args.date_from)
    date_to = parse_date(args.date_to)
    start_utc, end_utc = utc_range(
        date_from,
        date_to,
        args.timezone,
    )
    credentials = VaultElasticClient().get_es_credentials(
        args.elastic_secret
    )
    base = credentials["url"].rstrip("/")
    auth = (credentials["username"], credentials["password"])
    filters = [
        {
            "range": {
                "@timestamp": {
                    "gte": start_utc,
                    "lt": end_utc,
                }
            }
        },
        {"term": {"EventType.keyword": args.event_type}},
        {"term": {"StationId.keyword": args.station_id}},
        {"exists": {"field": "SkuId.keyword"}},
    ]
    if args.source:
        filters.append({"term": {"Source.keyword": args.source}})

    skus: list[str] = []
    after: dict[str, object] | None = None
    total_records: int | None = None
    session = requests.Session()
    session.headers.update(
        {"Accept": "application/json", "Content-Type": "application/json"}
    )
    while True:
        composite: dict[str, object] = {
            "size": 1000,
            "sources": [
                {"sku": {"terms": {"field": "SkuId.keyword"}}}
            ],
        }
        if after:
            composite["after"] = after
        body = {
            "size": 0,
            "track_total_hits": True,
            "query": {"bool": {"filter": filters}},
            "aggs": {"unique_skus": {"composite": composite}},
        }
        response = session.post(
            f"{base}/{args.index}/_search",
            auth=auth,
            json=body,
            timeout=120,
        )
        if not response.ok:
            raise RuntimeError(
                f"Elasticsearch HTTP {response.status_code}: "
                f"{response.text[:2000]}"
            )
        payload = response.json()
        failures = payload.get("_shards", {}).get("failures", [])
        if failures:
            raise RuntimeError(f"Elasticsearch shard failures: {failures[:3]}")
        if total_records is None:
            total = payload["hits"]["total"]
            total_records = (
                int(total["value"]) if isinstance(total, dict) else int(total)
            )
        aggregation = payload.get("aggregations", {}).get("unique_skus")
        if aggregation is None:
            raise RuntimeError(
                "Elasticsearch response omitted the unique_skus aggregation"
            )
        buckets = aggregation.get("buckets", [])
        skus.extend(
            str(bucket["key"]["sku"]).strip()
            for bucket in buckets
            if str(bucket["key"]["sku"]).strip()
        )
        after = aggregation.get("after_key")
        if not buckets or not after:
            break

    unique_skus = sorted(set(skus))
    result = {
        "source_type": "metric_events",
        "preferred_source": True,
        "index": args.index,
        "elastic_secret": args.elastic_secret,
        "event_type": args.event_type,
        "source": args.source,
        "station_id": args.station_id,
        "timezone": args.timezone,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "utc_range": [start_utc, end_utc],
        "metric_records": total_records or 0,
        "unique_skus": len(unique_skus),
        "sku_values": unique_skus,
        "fallback_used": False,
        "fallback_approval_required": (
            "Explicit user approval is required before using any non-metric "
            "SKU source."
        ),
    }
    return result


def export(args: argparse.Namespace, result: dict[str, object]) -> None:
    if not args.approved_write:
        raise PermissionError(
            "Refusing to write SKU artifacts without --approved-write"
        )
    output = args.output.expanduser().resolve()
    summary = (
        args.summary_output.expanduser().resolve()
        if args.summary_output
        else output.with_name(f"{output.stem}.summary.json")
    )
    existing = [path for path in (output, summary) if path.exists()]
    if existing and not args.overwrite:
        raise FileExistsError(
            "Refusing to overwrite without --overwrite: "
            + ", ".join(str(path) for path in existing)
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "".join(f"{sku}\n" for sku in result["sku_values"]),
        encoding="utf-8",
    )
    summary_payload = {
        key: value for key, value in result.items() if key != "sku_values"
    }
    summary_payload["sku_output"] = str(output)
    summary.write_text(
        json.dumps(summary_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to", required=True)
    parser.add_argument("--station-id", required=True)
    parser.add_argument("--source", default="RES")
    parser.add_argument("--timezone", default="America/New_York")
    parser.add_argument("--index", default=DEFAULT_INDEX)
    parser.add_argument("--elastic-secret", default=DEFAULT_ELASTIC_SECRET)
    parser.add_argument("--event-type", default=DEFAULT_EVENT_TYPE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser(
        "plan",
        help="Read metrics and report exact record and unique-SKU counts.",
    )
    add_common_arguments(plan_parser)

    export_parser = subparsers.add_parser(
        "export",
        help="Write the metric-derived unique SKU list and provenance.",
    )
    add_common_arguments(export_parser)
    export_parser.add_argument("--output", type=Path, required=True)
    export_parser.add_argument("--summary-output", type=Path)
    export_parser.add_argument("--overwrite", action="store_true")
    export_parser.add_argument("--approved-write", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = query_unique_skus(args)
    public_result = {
        key: value for key, value in result.items() if key != "sku_values"
    }
    if result["unique_skus"] == 0:
        public_result["status"] = "no_metric_skus"
        public_result["next_step"] = (
            "Stop and ask the user for approval before querying a fallback "
            "source."
        )
    else:
        public_result["status"] = "metric_skus_found"
    if args.command == "export":
        export(args, result)
        public_result["sku_output"] = str(args.output.expanduser().resolve())
    print(json.dumps(public_result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2)
