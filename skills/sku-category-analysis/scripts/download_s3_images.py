#!/usr/bin/env python3
"""Plan and run a size-validated, resumable S3 image download."""

from __future__ import annotations

import argparse
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time
from urllib.parse import urlparse
import uuid


DEFAULT_WORKERS = 16


@dataclass(frozen=True)
class Entry:
    uri: str
    bucket: str
    key: str
    expected_size: int
    relative_path: Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.lstrip("/"):
        raise ValueError(f"Invalid S3 URI: {uri}")
    return parsed.netloc, parsed.path.lstrip("/")


def relative_output_path(key: str) -> Path:
    key_path = Path(key)
    filename = key_path.name
    date = key_path.parent.name
    if not filename or filename in {".", ".."}:
        raise ValueError(f"Invalid S3 key filename: {key}")
    if not date or date in {".", ".."}:
        raise ValueError(f"Invalid S3 key date directory: {key}")
    return Path(date) / filename


def read_manifest(path: Path) -> list[str]:
    uris: list[str] = []
    with path.open(newline="", encoding="utf-8") as stream:
        for row in csv.reader(stream):
            if not row:
                continue
            value = row[0].strip()
            if value.startswith("s3://"):
                uris.append(value)
    if not uris:
        raise ValueError(f"No S3 URIs found in the first column of {path}")
    if len(uris) != len(set(uris)):
        raise ValueError("The manifest contains duplicate S3 URIs")
    return uris


def read_expected_sizes(path: Path) -> dict[str, int]:
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        required = {"s3_uri", "s3_size_bytes"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"Mapping CSV is missing columns: {sorted(missing)}"
            )
        result: dict[str, int] = {}
        for row in reader:
            uri = row["s3_uri"].strip()
            if not uri:
                continue
            size_text = row["s3_size_bytes"].strip()
            if not size_text:
                raise ValueError(f"Missing expected size for {uri}")
            size = int(size_text)
            if size <= 0:
                raise ValueError(f"Expected size must be positive for {uri}")
            if uri in result and result[uri] != size:
                raise ValueError(f"Conflicting expected sizes for {uri}")
            result[uri] = size
    return result


def build_entries(manifest: Path, mapping: Path) -> list[Entry]:
    expected_sizes = read_expected_sizes(mapping)
    entries: list[Entry] = []
    destinations: dict[Path, str] = {}
    for uri in read_manifest(manifest):
        if uri not in expected_sizes:
            raise ValueError(f"Manifest URI is absent from mapping CSV: {uri}")
        bucket, key = parse_s3_uri(uri)
        relative_path = relative_output_path(key)
        other_uri = destinations.get(relative_path)
        if other_uri is not None and other_uri != uri:
            raise ValueError(
                f"Multiple S3 URIs map to {relative_path}: "
                f"{other_uri}, {uri}"
            )
        destinations[relative_path] = uri
        entries.append(
            Entry(
                uri=uri,
                bucket=bucket,
                key=key,
                expected_size=expected_sizes[uri],
                relative_path=relative_path,
            )
        )
    return entries


def existing_anchor(path: Path) -> Path:
    candidate = path
    while not candidate.exists():
        if candidate.parent == candidate:
            raise FileNotFoundError(f"No existing parent for {path}")
        candidate = candidate.parent
    return candidate


def aws_identity() -> dict[str, str]:
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise RuntimeError(
            "boto3 is required; use the workspace annotation virtualenv"
        ) from exc

    client = boto3.client(
        "sts",
        config=Config(retries={"max_attempts": 5, "mode": "adaptive"}),
    )
    identity = client.get_caller_identity()
    return {
        "account": str(identity["Account"]),
        "arn": str(identity["Arn"]),
    }


def make_plan(
    manifest: Path,
    mapping: Path,
    output_dir: Path,
    workers: int,
) -> tuple[dict[str, object], list[Entry]]:
    manifest = manifest.expanduser().resolve()
    mapping = mapping.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if not manifest.is_file():
        raise FileNotFoundError(f"Manifest does not exist: {manifest}")
    if not mapping.is_file():
        raise FileNotFoundError(f"Mapping CSV does not exist: {mapping}")
    if workers < 1:
        raise ValueError("--workers must be positive")

    entries = build_entries(manifest, mapping)
    pending: list[Entry] = []
    existing_valid = 0
    existing_invalid = 0
    for entry in entries:
        destination = output_dir / entry.relative_path
        if destination.is_file() and destination.stat().st_size == entry.expected_size:
            existing_valid += 1
        else:
            pending.append(entry)
            if destination.exists():
                existing_invalid += 1

    total_bytes = sum(entry.expected_size for entry in entries)
    pending_bytes = sum(entry.expected_size for entry in pending)
    disk = shutil.disk_usage(existing_anchor(output_dir))
    identity = aws_identity()
    identity_payload = {
        "manifest_sha256": sha256_file(manifest),
        "mapping_sha256": sha256_file(mapping),
        "output_dir": str(output_dir),
        "aws_account": identity["account"],
        "aws_arn": identity["arn"],
        "pending": [
            [entry.uri, entry.expected_size, str(entry.relative_path)]
            for entry in pending
        ],
    }
    plan_id = hashlib.sha256(
        json.dumps(
            identity_payload,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    plan = {
        "plan_id": plan_id,
        "manifest": str(manifest),
        "mapping": str(mapping),
        "output_dir": str(output_dir),
        "workers": workers,
        "aws_identity": identity,
        "total_images": len(entries),
        "total_bytes": total_bytes,
        "total_gb": round(total_bytes / 1_000_000_000, 3),
        "total_gib": round(total_bytes / 1024**3, 3),
        "existing_valid_images": existing_valid,
        "existing_invalid_images": existing_invalid,
        "pending_images": len(pending),
        "pending_bytes": pending_bytes,
        "pending_gb": round(pending_bytes / 1_000_000_000, 3),
        "pending_gib": round(pending_bytes / 1024**3, 3),
        "free_bytes": disk.free,
        "free_gb": round(disk.free / 1_000_000_000, 3),
        "free_gib": round(disk.free / 1024**3, 3),
        "projected_free_bytes": disk.free - pending_bytes,
        "projected_free_gb": round(
            (disk.free - pending_bytes) / 1_000_000_000, 3
        ),
        "projected_free_gib": round(
            (disk.free - pending_bytes) / 1024**3, 3
        ),
        "space_sufficient": disk.free >= pending_bytes,
        "download_started": False,
        "approval_required": True,
    }
    return plan, pending


def make_s3_client():
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        config=Config(retries={"max_attempts": 10, "mode": "adaptive"}),
    )


def download_one(s3, entry: Entry, output_dir: Path) -> dict[str, object]:
    destination = output_dir / entry.relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and destination.stat().st_size == entry.expected_size:
        return {"uri": entry.uri, "status": "already_valid"}

    temporary = destination.with_name(
        f".{destination.name}.part-{uuid.uuid4().hex}"
    )
    try:
        s3.download_file(entry.bucket, entry.key, str(temporary))
        actual_size = temporary.stat().st_size
        if actual_size != entry.expected_size:
            raise RuntimeError(
                f"size mismatch: expected {entry.expected_size}, "
                f"downloaded {actual_size}"
            )
        os.replace(temporary, destination)
        return {
            "uri": entry.uri,
            "status": "downloaded",
            "path": str(destination),
            "bytes": actual_size,
        }
    except Exception as exc:
        if temporary.exists():
            temporary.unlink()
        return {
            "uri": entry.uri,
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
        }


def write_results(
    output_dir: Path,
    plan: dict[str, object],
    results: list[dict[str, object]],
) -> None:
    failures = [row for row in results if row["status"] == "failed"]
    failure_path = output_dir / ".download-failures.csv"
    with failure_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["uri", "status", "error"])
        writer.writeheader()
        writer.writerows(
            {
                "uri": row["uri"],
                "status": row["status"],
                "error": row.get("error", ""),
            }
            for row in failures
        )

    summary = {
        **plan,
        "download_started": True,
        "downloaded_images": sum(
            row["status"] == "downloaded" for row in results
        ),
        "already_valid_images": sum(
            row["status"] == "already_valid" for row in results
        ),
        "failed_images": len(failures),
        "failure_report": str(failure_path),
    }
    (output_dir / ".download-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def run_download(args: argparse.Namespace) -> int:
    plan, pending = make_plan(
        args.input_csv,
        args.mapping_csv,
        args.output_dir,
        args.workers,
    )
    print(json.dumps(plan, indent=2, sort_keys=True), flush=True)
    if not args.approved_download:
        raise PermissionError(
            "Refusing to download without --approved-download"
        )
    if not args.plan_id:
        raise PermissionError(
            "Refusing to download without the explicitly approved --plan-id"
        )
    if args.plan_id != plan["plan_id"]:
        raise PermissionError(
            "The approved plan ID does not match the current download plan; "
            "run plan again and obtain fresh approval"
        )
    if not plan["space_sufficient"]:
        raise RuntimeError("Insufficient free disk space for pending downloads")

    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if not pending:
        write_results(output_dir, plan, [])
        print("No downloads are pending.")
        return 0

    s3 = make_s3_client()
    results: list[dict[str, object]] = []
    lock = threading.Lock()
    completed = 0
    last_report = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(download_one, s3, entry, output_dir): entry
            for entry in pending
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            with lock:
                completed += 1
                now = time.monotonic()
                if (
                    completed == len(pending)
                    or completed % 100 == 0
                    or now - last_report >= 30
                ):
                    failures = sum(
                        row["status"] == "failed" for row in results
                    )
                    print(
                        f"progress={completed}/{len(pending)} "
                        f"failures={failures}",
                        flush=True,
                    )
                    last_report = now

    write_results(output_dir, plan, results)
    failures = sum(row["status"] == "failed" for row in results)
    if failures:
        print(f"Download completed with {failures} failures.", file=sys.stderr)
        return 1
    print(f"Downloaded and validated {len(pending)} images to {output_dir}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ("plan", "download"):
        subparser = subparsers.add_parser(name)
        subparser.add_argument("--input-csv", type=Path, required=True)
        subparser.add_argument("--mapping-csv", type=Path, required=True)
        subparser.add_argument("--output-dir", type=Path, required=True)
        subparser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
        if name == "download":
            subparser.add_argument("--plan-id")
            subparser.add_argument("--approved-download", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "plan":
        plan, _ = make_plan(
            args.input_csv,
            args.mapping_csv,
            args.output_dir,
            args.workers,
        )
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    if args.command == "download":
        return run_download(args)
    raise AssertionError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2)
