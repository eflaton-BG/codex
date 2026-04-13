#!/usr/bin/env python3
"""Copy REV image files referenced by a CSV using resumable batched kubectl exec tar operations."""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence


SUCCESS_STATUSES = {"ok", "exists"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read a CSV containing png_filename values, deduplicate them, write local manifests, "
            "and copy the image files in resumable batches using kubectl exec plus tar."
        )
    )
    parser.add_argument("--csv", required=True, help="Input CSV with a png_filename column.")
    parser.add_argument("--output-dir", required=True, help="Directory where image files will be copied.")
    parser.add_argument("--pod", help="Pod name that contains /var/bg/image_data or the selected remote root.")
    parser.add_argument("--namespace", help="Kubernetes namespace. Optional.")
    parser.add_argument("--container", help="Container name for kubectl exec. Optional.")
    parser.add_argument("--context", help="kubectl context. Optional.")
    parser.add_argument("--remote-root", default="/var/bg/image_data", help="Remote root that contains the image_data tree. Default: /var/bg/image_data.")
    parser.add_argument("--kubectl", default="/usr/local/bin/kubectl", help="Absolute kubectl path. Default: /usr/local/bin/kubectl.")
    parser.add_argument("--local-tar", default="/usr/bin/tar", help="Absolute local tar path. Default: /usr/bin/tar.")
    parser.add_argument("--state-dir", help="State directory for manifests and logs. Default: <output-dir>/.copy-state")
    parser.add_argument("--retries", type=int, default=999999, help="Retries per batch after the first attempt. Default: 999999.")
    parser.add_argument("--batch-size", type=int, default=10, help="Number of files to transfer per batch. Default: 10.")
    parser.add_argument("--limit", type=int, help="Only process the first N pending files.")
    parser.add_argument("--manifest-only", action="store_true", help="Write manifests but do not run downloads.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite files that already exist locally.")
    args = parser.parse_args()
    if not args.manifest_only and not args.pod:
        parser.error("--pod is required unless --manifest-only is set.")
    if args.batch_size <= 0:
        parser.error("--batch-size must be positive.")
    return args


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def state_dir_for(args: argparse.Namespace, output_dir: Path) -> Path:
    if args.state_dir:
        return Path(args.state_dir)
    return output_dir / ".copy-state"


def read_unique_pngs(csv_path: Path) -> list[str]:
    seen: set[str] = set()
    values: list[str] = []
    with csv_path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if "png_filename" not in (reader.fieldnames or []):
            raise SystemExit(f"CSV does not contain png_filename column: {csv_path}")
        for row in reader:
            value = (row.get("png_filename") or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            values.append(value)
    return values


def load_latest_statuses(results_path: Path) -> dict[str, dict]:
    statuses: dict[str, dict] = {}
    if not results_path.exists():
        return statuses
    with results_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            statuses[record["png_filename"]] = record
    return statuses


def write_list(path: Path, values: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for value in values:
            handle.write(value)
            handle.write("\n")


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True))
        handle.write("\n")


def refresh_manifests(state_dir: Path, all_pngs: list[str], latest_statuses: dict[str, dict]) -> tuple[list[str], list[str], list[str]]:
    copied = [png for png in all_pngs if latest_statuses.get(png, {}).get("status") in SUCCESS_STATUSES]
    failed = [png for png in all_pngs if latest_statuses.get(png, {}).get("status") == "failed"]
    pending = [png for png in all_pngs if png not in set(copied)]
    write_list(state_dir / "all_pngs.txt", all_pngs)
    write_list(state_dir / "copied_pngs.txt", copied)
    write_list(state_dir / "failed_pngs.txt", failed)
    write_list(state_dir / "pending_pngs.txt", pending)
    return copied, failed, pending


def pod_spec(namespace: str | None, pod: str) -> list[str]:
    parts: list[str] = []
    if namespace:
        parts.extend(["-n", namespace])
    parts.append(pod)
    return parts


def chunked(values: Sequence[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield list(values[index : index + size])


def batch_paths(output_dir: Path, png_filenames: Sequence[str]) -> list[Path]:
    return [output_dir / png_filename for png_filename in png_filenames]


def remove_local_targets(paths: Sequence[Path]) -> None:
    for path in paths:
        if path.exists():
            path.unlink()


def ensure_parent_dirs(paths: Sequence[Path]) -> None:
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)


def kubectl_exec_command(args: argparse.Namespace, png_filenames: Sequence[str]) -> list[str]:
    command = [args.kubectl]
    if args.context:
        command.extend(["--context", args.context])
    command.append("exec")
    if args.container:
        command.extend(["-c", args.container])
    command.extend(pod_spec(args.namespace, args.pod))
    command.extend(["--", "tar", "cf", "-", "-C", args.remote_root])
    command.extend(png_filenames)
    return command


def local_extract_command(args: argparse.Namespace, output_dir: Path) -> list[str]:
    return [args.local_tar, "xf", "-", "-C", str(output_dir)]


def batch_status_records(
    png_filenames: Sequence[str],
    local_paths: Sequence[Path],
    exec_command: list[str],
    extract_command: list[str],
    status: str,
    attempt: int,
    returncode: int,
    stdout: str,
    stderr: str,
) -> list[dict]:
    timestamp = utc_now()
    records: list[dict] = []
    for png_filename, local_path in zip(png_filenames, local_paths, strict=True):
        records.append(
            {
                "timestamp": timestamp,
                "png_filename": png_filename,
                "local_target": str(local_path),
                "exec_command": exec_command,
                "extract_command": extract_command,
                "status": status,
                "attempt": attempt,
                "returncode": returncode,
                "stdout": stdout[-4000:],
                "stderr": stderr[-4000:],
            }
        )
    return records


def run_batch_once(args: argparse.Namespace, png_filenames: Sequence[str], output_dir: Path) -> tuple[int, str, str, list[Path], list[str], list[str]]:
    local_paths = batch_paths(output_dir, png_filenames)
    ensure_parent_dirs(local_paths)
    remove_local_targets(local_paths)

    exec_command = kubectl_exec_command(args, png_filenames)
    extract_command = local_extract_command(args, output_dir)

    exec_process = subprocess.Popen(exec_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert exec_process.stdout is not None
    assert exec_process.stderr is not None
    extract_completed = subprocess.run(extract_command, stdin=exec_process.stdout, capture_output=True)
    exec_process.stdout.close()
    exec_stderr = exec_process.stderr.read().decode("utf-8", errors="replace")
    exec_process.stderr.close()
    exec_returncode = exec_process.wait()

    extract_stdout = extract_completed.stdout.decode("utf-8", errors="replace")
    extract_stderr = extract_completed.stderr.decode("utf-8", errors="replace")
    combined_stdout = extract_stdout
    combined_stderr = exec_stderr + extract_stderr
    returncode = exec_returncode if exec_returncode != 0 else extract_completed.returncode
    return returncode, combined_stdout, combined_stderr, local_paths, exec_command, extract_command


def copy_batch(args: argparse.Namespace, png_filenames: Sequence[str], output_dir: Path, results_path: Path) -> list[dict]:
    attempt = 0
    max_attempts = None if args.retries < 0 else args.retries + 1
    while max_attempts is None or attempt < max_attempts:
        attempt += 1
        returncode, stdout, stderr, local_paths, exec_command, extract_command = run_batch_once(args, png_filenames, output_dir)
        if returncode == 0:
            records = batch_status_records(
                png_filenames,
                local_paths,
                exec_command,
                extract_command,
                "ok",
                attempt,
                returncode,
                stdout,
                stderr,
            )
            for record in records:
                append_jsonl(results_path, record)
            return records
        remove_local_targets(local_paths)
        if max_attempts is not None and attempt >= max_attempts:
            records = batch_status_records(
                png_filenames,
                local_paths,
                exec_command,
                extract_command,
                "failed",
                attempt,
                returncode,
                stdout,
                stderr,
            )
            for record in records:
                append_jsonl(results_path, record)
            return records

    raise AssertionError("unreachable")


def main() -> int:
    args = parse_args()
    csv_path = Path(args.csv)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    state_dir = state_dir_for(args, output_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    results_path = state_dir / "results.jsonl"

    all_pngs = read_unique_pngs(csv_path)
    latest_statuses = load_latest_statuses(results_path)
    copied, failed, pending = refresh_manifests(state_dir, all_pngs, latest_statuses)

    summary = {
        "timestamp": utc_now(),
        "csv": str(csv_path),
        "output_dir": str(output_dir),
        "state_dir": str(state_dir),
        "total_unique_pngs": len(all_pngs),
        "already_copied": len(copied),
        "pending": len(pending),
        "failed": len(failed),
        "manifest_only": args.manifest_only,
        "batch_size": args.batch_size,
        "retries": args.retries,
    }
    print(json.dumps(summary, indent=2), file=sys.stderr)

    if args.manifest_only:
        return 0

    if args.limit is not None:
        pending = pending[: args.limit]

    batches = list(chunked(pending, args.batch_size))
    for batch_index, batch in enumerate(batches, start=1):
        print(f"[batch {batch_index}/{len(batches)}] {len(batch)} files", file=sys.stderr)
        records = copy_batch(args, batch, output_dir, results_path)
        for record in records:
            latest_statuses[record["png_filename"]] = record
        copied, failed, pending_all = refresh_manifests(state_dir, all_pngs, latest_statuses)
        print(
            json.dumps(
                {
                    "batch": batch_index,
                    "status": records[0]["status"] if records else "unknown",
                    "copied": len(copied),
                    "failed": len(failed),
                    "remaining": len(pending_all),
                }
            ),
            file=sys.stderr,
        )

    latest_statuses = load_latest_statuses(results_path)
    copied, failed, pending = refresh_manifests(state_dir, all_pngs, latest_statuses)
    final_summary = {
        "timestamp": utc_now(),
        "total_unique_pngs": len(all_pngs),
        "copied": len(copied),
        "failed": len(failed),
        "pending": len(pending),
        "results": str(results_path),
    }
    print(json.dumps(final_summary, indent=2), file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
