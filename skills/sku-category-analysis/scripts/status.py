#!/usr/bin/env python3
"""Read local preparation progress without provider calls."""
import argparse
import json
from datetime import datetime
from pathlib import Path


def status(job):
    root = job.expanduser().resolve() / "manifest"
    inventory = json.loads((root / "unique-skus.summary.json").read_text())
    work = root / ".fallback-work"
    days = list(work.glob("*/complete.json"))
    skus = set()
    for path in days:
        rows = json.loads(path.read_text())["candidates"]
        skus.update(row["SkuId"] for row in rows)
    result = {
        "timestamp": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
        "completed_days": len(days),
        "candidate_skus": len(skus),
        "inventory_skus": inventory["unique_skus"],
        "validated_log_slices": len(list(work.glob("*/*-*.json"))),
        "scope": "preparation_only",
    }
    for name, path in (
        ("fallback_error", work / "last-error.json"),
        ("s3_finalization", root / "s3-finalization.status.json"),
        ("s3_summary", root / "s3-validation.summary.json"),
    ):
        if path.exists():
            data = json.loads(path.read_text())
            result[name] = {key: data[key] for key in (
                "phase", "error_type", "validated_skus", "unique_s3_objects",
                "rejected_skus", "s3_total_gb", "s3_total_gib",
            ) if key in data}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-dir", type=Path, required=True)
    status(parser.parse_args().job_dir)
