#!/usr/bin/env python3
"""Copy REV image files referenced by a CSV using resumable rsync or kubectl cp transfers."""

from __future__ import annotations

import argparse
import csv
import json
import os
import shlex
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


SUCCESS_STATUSES = {"ok", "exists"}
SYSTEMIC_POD_NOT_FOUND = "Error from server (NotFound): pods"
KUBE_AUTH_FAILURE_MARKERS = (
    "You must be logged in to the server",
    "the server has asked for the client to provide credentials",
)
REMOTE_RSYNC_MISSING_MARKERS = (
    "executable file not found",
    "No such file or directory",
    "not found",
)
RSYNC_REMOTE_HOST = "pittston-pod"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read a CSV containing png_filename values, deduplicate them, write local manifests, "
            "and copy the image files using resumable rsync over kubectl exec."
        )
    )
    parser.add_argument("--csv", required=True, help="Input CSV with a png_filename column.")
    parser.add_argument("--output-dir", required=True, help="Directory where image files will be copied.")
    parser.add_argument("--pod", help="Pod name that contains /var/bg/image_data or the selected remote root.")
    parser.add_argument("--namespace", help="Kubernetes namespace. Optional.")
    parser.add_argument("--container", help="Container name for kubectl exec/cp. Optional.")
    parser.add_argument(
        "--context",
        default="k8s/washington-pit-context",
        help="kubectl context. Default: k8s/washington-pit-context.",
    )
    parser.add_argument(
        "--remote-root",
        default="/var/bg/image_data",
        help="Remote root that contains the image_data tree. Default: /var/bg/image_data.",
    )
    parser.add_argument(
        "--kubectl",
        default="/usr/local/bin/kubectl",
        help="Absolute kubectl path. Default: /usr/local/bin/kubectl.",
    )
    parser.add_argument(
        "--transfer-mode",
        choices=("rsync", "kubectl-cp"),
        default="rsync",
        help="Transfer backend. Default: rsync.",
    )
    parser.add_argument(
        "--rsync",
        default="/usr/bin/rsync",
        help="Absolute local rsync path. Default: /usr/bin/rsync.",
    )
    parser.add_argument(
        "--remote-rsync",
        default="/usr/bin/rsync",
        help="Absolute rsync path inside the pod. Default: /usr/bin/rsync.",
    )
    parser.add_argument(
        "--state-dir",
        help="State directory for manifests and logs. Default: <output-dir>/.copy-state",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=999999,
        help="Pass-through value for kubectl cp --retries. Default: 999999.",
    )
    parser.add_argument("--limit", type=int, help="Only process the first N pending files.")
    parser.add_argument("--manifest-only", action="store_true", help="Write manifests but do not run downloads.")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-copy files even if they were previously marked successful.",
    )
    args = parser.parse_args()
    if not args.manifest_only and not args.pod:
        parser.error("--pod is required unless --manifest-only is set.")
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
            png_filename = record.get("png_filename")
            if png_filename:
                statuses[png_filename] = record
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
    copied_set = set(copied)
    pending = [png for png in all_pngs if png not in copied_set]
    write_list(state_dir / "all_pngs.txt", all_pngs)
    write_list(state_dir / "copied_pngs.txt", copied)
    write_list(state_dir / "failed_pngs.txt", failed)
    write_list(state_dir / "pending_pngs.txt", pending)
    return copied, failed, pending


def kubectl_exec_prefix(args: argparse.Namespace) -> list[str]:
    command = [args.kubectl]
    if args.context:
        command.extend(["--context", args.context])
    command.append("exec")
    command.append("-i")
    if args.namespace:
        command.extend(["-n", args.namespace])
    if args.container:
        command.extend(["-c", args.container])
    command.append(args.pod)
    command.append("--")
    return command


def ensure_rsync_rsh(args: argparse.Namespace, state_dir: Path) -> Path:
    wrapper_path = state_dir / "kubectl_rsync_rsh.sh"
    command = kubectl_exec_prefix(args)
    quoted = " ".join(shlex.quote(part) for part in command)
    script = (
        "#!/bin/sh\n"
        "host=\"$1\"\n"
        "shift\n"
        f"exec {quoted} \"$@\"\n"
    )
    wrapper_path.write_text(script, encoding="utf-8")
    wrapper_path.chmod(wrapper_path.stat().st_mode | stat.S_IXUSR)
    return wrapper_path


def kubectl_cp_command(args: argparse.Namespace, png_filename: str, local_target: Path) -> list[str]:
    remote_path = args.remote_root.rstrip("/") + "/" + png_filename
    command = [args.kubectl]
    if args.context:
        command.extend(["--context", args.context])
    if args.namespace:
        command.extend(["-n", args.namespace])
    command.extend(["cp", "--retries", str(args.retries)])
    if args.container:
        command.extend(["-c", args.container])
    command.extend([f"{args.pod}:{remote_path}", str(local_target)])
    return command


def rsync_command(args: argparse.Namespace, png_filename: str, local_target: Path, state_dir: Path) -> list[str]:
    remote_path = args.remote_root.rstrip("/") + "/" + png_filename
    rsh_path = ensure_rsync_rsh(args, state_dir)
    command = [
        args.rsync,
        "--archive",
        "--partial",
        "--append-verify",
        "--inplace",
        "--rsh",
        str(rsh_path),
        "--rsync-path",
        args.remote_rsync,
        f"{RSYNC_REMOTE_HOST}:{remote_path}",
        str(local_target),
    ]
    return command


def build_record(
    png_filename: str,
    local_target: Path,
    copy_command: list[str],
    status: str,
    returncode: int,
    stdout: str,
    stderr: str,
    *,
    removed_partial: bool = False,
    preserved_partial: bool = False,
    transfer_mode: str,
) -> dict:
    record = {
        "timestamp": utc_now(),
        "png_filename": png_filename,
        "local_target": str(local_target),
        "copy_command": copy_command,
        "status": status,
        "attempt": 1,
        "returncode": returncode,
        "stdout": stdout[-4000:],
        "stderr": stderr[-4000:],
        "transfer_mode": transfer_mode,
    }
    if removed_partial:
        record["removed_partial"] = True
    if preserved_partial:
        record["preserved_partial"] = True
    return record


def transfer_command_for(args: argparse.Namespace, png_filename: str, local_target: Path, state_dir: Path) -> list[str]:
    if args.transfer_mode == "rsync":
        return rsync_command(args, png_filename, local_target, state_dir)
    return kubectl_cp_command(args, png_filename, local_target)


def copy_one(args: argparse.Namespace, png_filename: str, output_dir: Path, state_dir: Path) -> dict:
    local_target = output_dir / png_filename
    local_target.parent.mkdir(parents=True, exist_ok=True)

    if args.overwrite and local_target.exists():
        local_target.unlink()
    elif args.transfer_mode == "kubectl-cp" and local_target.exists():
        local_target.unlink()

    copy_command = transfer_command_for(args, png_filename, local_target, state_dir)
    completed = subprocess.run(copy_command, capture_output=True, text=True)
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""

    if completed.returncode == 0 and local_target.exists():
        return build_record(
            png_filename,
            local_target,
            copy_command,
            "ok",
            completed.returncode,
            stdout,
            stderr,
            transfer_mode=args.transfer_mode,
        )

    if args.transfer_mode == "rsync":
        return build_record(
            png_filename,
            local_target,
            copy_command,
            "failed",
            completed.returncode,
            stdout,
            stderr,
            preserved_partial=local_target.exists(),
            transfer_mode=args.transfer_mode,
        )

    removed_partial = False
    if local_target.exists():
        local_target.unlink()
        removed_partial = True

    return build_record(
        png_filename,
        local_target,
        copy_command,
        "failed",
        completed.returncode,
        stdout,
        stderr,
        removed_partial=removed_partial,
        transfer_mode=args.transfer_mode,
    )


def remote_rsync_missing(args: argparse.Namespace, record: dict) -> bool:
    if args.transfer_mode != "rsync" or record["status"] != "failed":
        return False
    stderr = record.get("stderr", "")
    stdout = record.get("stdout", "")
    combined = f"{stderr}\n{stdout}"
    return args.remote_rsync in combined and any(marker in combined for marker in REMOTE_RSYNC_MISSING_MARKERS)


def kube_auth_failed(record: dict) -> bool:
    if record["status"] != "failed":
        return False
    stderr = record.get("stderr", "")
    stdout = record.get("stdout", "")
    combined = f"{stderr}\n{stdout}"
    return any(marker in combined for marker in KUBE_AUTH_FAILURE_MARKERS)


def main() -> int:
    args = parse_args()
    csv_path = Path(args.csv)
    output_dir = Path(args.output_dir)
    state_dir = state_dir_for(args, output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    all_pngs = read_unique_pngs(csv_path)
    results_path = state_dir / "results.jsonl"
    latest_statuses = load_latest_statuses(results_path)
    copied, failed, pending = refresh_manifests(state_dir, all_pngs, latest_statuses)

    if args.overwrite:
        pending = list(all_pngs)

    if args.limit is not None:
        pending = pending[: args.limit]

    summary = {
        "timestamp": utc_now(),
        "total_unique_pngs": len(all_pngs),
        "already_copied": len(copied),
        "failed": len(failed),
        "pending": len(pending),
        "output_dir": str(output_dir),
        "state_dir": str(state_dir),
        "manifest_only": args.manifest_only,
        "retries": args.retries,
        "transfer_mode": args.transfer_mode,
        "context": args.context,
    }
    print(json.dumps(summary, sort_keys=True))

    if args.manifest_only:
        return 0

    for index, png_filename in enumerate(pending, start=1):
        record = copy_one(args, png_filename, output_dir, state_dir)
        append_jsonl(results_path, record)
        latest_statuses[png_filename] = record
        copied, failed, pending_after = refresh_manifests(state_dir, all_pngs, latest_statuses)
        progress = {
            "timestamp": utc_now(),
            "index": index,
            "total": len(pending),
            "png_filename": png_filename,
            "status": record["status"],
            "already_copied": len(copied),
            "failed": len(failed),
            "pending": len(pending_after),
            "transfer_mode": args.transfer_mode,
        }
        print(json.dumps(progress, sort_keys=True))
        sys.stdout.flush()

        if kube_auth_failed(record):
            print(
                json.dumps(
                    {
                        "timestamp": utc_now(),
                        "abort_reason": "kube-auth-failed",
                        "context": args.context,
                        "pod": args.pod,
                        "png_filename": png_filename,
                        "action": "refresh kubectl auth and rerun",
                    },
                    sort_keys=True,
                )
            )
            sys.stdout.flush()
            return 1

        if record["status"] == "failed" and SYSTEMIC_POD_NOT_FOUND in record["stderr"]:
            print(
                json.dumps(
                    {
                        "timestamp": utc_now(),
                        "abort_reason": "pod-not-found",
                        "pod": args.pod,
                        "png_filename": png_filename,
                    },
                    sort_keys=True,
                )
            )
            sys.stdout.flush()
            return 1

        if remote_rsync_missing(args, record):
            print(
                json.dumps(
                    {
                        "timestamp": utc_now(),
                        "abort_reason": "remote-rsync-missing",
                        "pod": args.pod,
                        "png_filename": png_filename,
                        "install_hint": "sudo apt-get update && sudo apt install rsync",
                    },
                    sort_keys=True,
                )
            )
            sys.stdout.flush()
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
