#!/usr/bin/env python3
"""Wait for the approved fallback, then verify S3 metadata only."""
import argparse
import fcntl
import json
import time
from datetime import datetime
from pathlib import Path

import prepare


def wait_for_mapping(root, wait, max_wait_seconds, poll_seconds=30):
    deadline = time.monotonic() + max_wait_seconds
    summary = root / "fallback-mapping.summary.json"
    error = root / ".fallback-work/last-error.json"
    last_completed = None
    while not summary.exists():
        if error.exists():
            raise RuntimeError(f"Fallback reported an error; inspect {error}")
        if not wait:
            raise FileNotFoundError("Fallback mapping is not complete")
        if time.monotonic() >= deadline:
            raise TimeoutError("Fallback wait expired; cached artifacts are preserved")
        completed = len(list((root / ".fallback-work").glob("*/complete.json")))
        if completed != last_completed:
            prepare.emit({"phase": "waiting-for-fallback", "completed_days": completed,
                          "checked_at": prepare.stamp(datetime.now(prepare.UTC))})
            last_completed = completed
        time.sleep(poll_seconds)
    return json.loads(summary.read_text())


def finalize(args):
    if not args.approved_write or not args.approved_s3_read:
        raise PermissionError("Requires artifact-write and S3 metadata-read approval")
    root = args.job_dir.expanduser().resolve() / "manifest"
    prepare.bounds(args.date_from, args.date_to)
    prepare.read_inventory(root, args)
    if args.max_wait_seconds <= 0:
        raise ValueError("Wait limit must be positive")
    with (root / ".s3-finalization.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        status = root / "s3-finalization.status.json"
        prepare.save(status, {
            "phase": "waiting-for-fallback", "openai_calls": 0, "image_downloads": 0,
            "started_at": prepare.stamp(datetime.now(prepare.UTC)),
        })
        try:
            mapping = wait_for_mapping(root, args.wait_for_fallback, args.max_wait_seconds)
            if (mapping["date_from"], mapping["date_to"]) != (args.date_from, args.date_to):
                raise ValueError("Fallback mapping date range differs")
            prepare.save(status, {
                "phase": "verifying-s3-metadata", "openai_calls": 0, "image_downloads": 0,
                "started_at": prepare.stamp(datetime.now(prepare.UTC)),
            })
            args.mapping_source = "fallback"
            prepare.validate_s3(args, root)
            result = json.loads((root / "s3-validation.summary.json").read_text())
            prepare.save(status, {
                "phase": "s3-verified", "openai_calls": 0, "image_downloads": 0,
                "validated_skus": result["validated_skus"],
                "unique_s3_objects": result["unique_s3_objects"],
                "finished_at": prepare.stamp(datetime.now(prepare.UTC)),
            })
        except Exception as error:
            prepare.save(status, {
                "phase": "stopped", "error_type": type(error).__name__,
                "openai_calls": 0, "image_downloads": 0,
                "stopped_at": prepare.stamp(datetime.now(prepare.UTC)),
            })
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to", required=True)
    parser.add_argument("--job-dir", required=True, type=Path)
    parser.add_argument("--approved-write", action="store_true")
    parser.add_argument("--approved-s3-read", action="store_true")
    parser.add_argument("--wait-for-fallback", action="store_true")
    parser.add_argument("--max-wait-seconds", type=int, default=6 * 3600)
    parser.add_argument("--s3-workers", type=int, default=8)
    finalize(parser.parse_args())


if __name__ == "__main__":
    main()
