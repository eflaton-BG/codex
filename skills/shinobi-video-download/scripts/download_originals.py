#!/usr/bin/env python3

import argparse
import base64
import hashlib
import json
import os
import re
import select
import subprocess
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


def parse_args():
    parser = argparse.ArgumentParser(
        description="Download complete original Shinobi recording segments locally."
    )
    parser.add_argument("--context", required=True)
    parser.add_argument("--station", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--timezone", required=True)
    parser.add_argument("--camera", action="append", dest="cameras")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--namespace", default="dvr")
    parser.add_argument("--pod", default="shinobi-0")
    parser.add_argument("--secret", default="shinobi-secrets")
    parser.add_argument("--local-port", type=int, default=18089)
    parser.add_argument("--remote-port", type=int, default=8080)
    parser.add_argument("--query-padding-minutes", type=int, default=6)
    parser.add_argument("--download-workers", type=int, default=4)
    parser.add_argument("--kubectl", default="/usr/local/bin/kubectl")
    return parser.parse_args()


def parse_time(value, timezone_name):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
    return parsed.astimezone(timezone.utc)


def parse_shinobi_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def format_query_time(value):
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def decode_secret(data, key):
    encoded = data.get(key)
    return base64.b64decode(encoded).decode() if encoded else None


def load_secret(args):
    raw = subprocess.check_output(
        [
            args.kubectl,
            "get",
            "secret",
            args.secret,
            "-n",
            args.namespace,
            "-o",
            "json",
            "--context",
            args.context,
        ],
        text=True,
    )
    return json.loads(raw).get("data", {})


def credential_pairs(secret_data):
    pairs = []
    for user_key, password_key in (
        ("username", "password"),
        ("admin.username", "admin.password"),
    ):
        username = decode_secret(secret_data, user_key)
        password = decode_secret(secret_data, password_key)
        if username and password:
            pairs.append((username, password))

    users_json = decode_secret(secret_data, "users.json")
    if users_json:
        pairs.extend(json.loads(users_json).items())
    return pairs


def authenticate(base_url, secret_data):
    for username, password in credential_pairs(secret_data):
        body = urllib.parse.urlencode(
            {
                "machineID": "codex-local-download",
                "mail": username,
                "pass": password,
                "function": "dash",
            }
        ).encode()
        request = urllib.request.Request(
            f"{base_url}/?json=true",
            data=body,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.load(response)
        except Exception:
            continue
        user = payload.get("$user") or payload.get("user")
        if isinstance(user, dict) and user.get("auth_token") and user.get("ke"):
            return user["auth_token"], user["ke"]
    raise RuntimeError("No stored Shinobi credential successfully authenticated")


def start_port_forward(args):
    process = subprocess.Popen(
        [
            args.kubectl,
            "port-forward",
            f"pod/{args.pod}",
            f"{args.local_port}:{args.remote_port}",
            "-n",
            args.namespace,
            "--address",
            "127.0.0.1",
            "--context",
            args.context,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.monotonic() + 20
    lines = []
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        ready, _, _ = select.select([process.stdout], [], [], 0.5)
        if not ready:
            continue
        line = process.stdout.readline()
        if line:
            lines.append(line.strip())
        if "Forwarding from 127.0.0.1" in line:
            return process
    process.terminate()
    raise RuntimeError(
        "Port-forward did not become ready: " + " | ".join(lines[-5:])
    )


def safe_slug(value):
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def videos_root():
    return (Path.home() / "Videos").resolve()


def resolve_output_dir(output_dir, station, start_local):
    root = videos_root()
    if output_dir is None:
        label = start_local.strftime("%Y%m%dT%H%M%S")
        output_dir = root / "Shinobi" / f"{safe_slug(station)}-{label}"
    else:
        output_dir = output_dir.expanduser().resolve()
    try:
        output_dir.relative_to(root)
    except ValueError as exc:
        raise SystemExit(
            f"Video output must be beneath the user's Videos directory: {root}"
        ) from exc
    return output_dir


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_file(url, output):
    partial = output.with_suffix(output.suffix + ".part")
    with urllib.request.urlopen(url, timeout=120) as response, partial.open("wb") as dst:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            dst.write(chunk)
    os.replace(partial, output)


def download_original(job):
    item = job["item"]
    output = job["output"]
    if not output.exists() or (
        item.get("size") and output.stat().st_size != int(item["size"])
    ):
        download_file(job["url"], output)
    return {
        "camera": job["camera"],
        "monitor_id": job["monitor_id"],
        "recording_start_utc": item["time"],
        "recording_end_utc": item["end"],
        "path": str(output.resolve()),
        "bytes": output.stat().st_size,
        "sha256": sha256_file(output),
    }


def download_jobs_parallel(download_jobs, workers):
    downloads = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(download_original, job) for job in download_jobs
        ]
        try:
            for future in as_completed(futures):
                downloads.append(future.result())
        except Exception:
            for future in futures:
                future.cancel()
            raise
    return sorted(
        downloads,
        key=lambda item: (item["camera"], item["recording_start_utc"]),
    )


def main():
    args = parse_args()
    if args.download_workers < 1:
        raise SystemExit("--download-workers must be at least 1")
    start_utc = parse_time(args.start, args.timezone)
    end_utc = parse_time(args.end, args.timezone)
    if end_utc <= start_utc:
        raise SystemExit("--end must be after --start")
    start_local = start_utc.astimezone(ZoneInfo(args.timezone))
    args.output_dir = resolve_output_dir(
        args.output_dir,
        args.station,
        start_local,
    )

    secret_data = load_secret(args)
    process = start_port_forward(args)
    base_url = f"http://127.0.0.1:{args.local_port}"
    try:
        auth_token, group_key = authenticate(base_url, secret_data)
        with urllib.request.urlopen(
            f"{base_url}/{auth_token}/monitor/{group_key}",
            timeout=20,
        ) as response:
            monitors = json.load(response)
        if isinstance(monitors, dict):
            monitors = monitors.get("monitors", [])

        patterns = args.cameras or ["Pick cell"]
        station = args.station.lower()
        selected = [
            monitor
            for monitor in monitors
            if station in str(monitor.get("name", "")).lower()
            and all(
                pattern.lower() in str(monitor.get("name", "")).lower()
                for pattern in patterns
            )
        ]
        if args.cameras:
            selected = [
                monitor
                for monitor in monitors
                if station in str(monitor.get("name", "")).lower()
                and any(
                    pattern.lower() in str(monitor.get("name", "")).lower()
                    for pattern in args.cameras
                )
            ]
        if not selected:
            raise RuntimeError(
                f"No monitor matched station {args.station!r} and cameras {patterns!r}"
            )

        originals_dir = args.output_dir / "originals"
        originals_dir.mkdir(parents=True, exist_ok=True)
        padded_start = start_utc - timedelta(minutes=args.query_padding_minutes)
        padded_end = end_utc + timedelta(minutes=args.query_padding_minutes)
        query = urllib.parse.urlencode(
            {
                "start": format_query_time(padded_start),
                "end": format_query_time(padded_end),
            }
        )

        download_jobs = []
        for monitor in selected:
            name = monitor["name"]
            monitor_id = monitor["mid"]
            with urllib.request.urlopen(
                f"{base_url}/{auth_token}/videos/{group_key}/{monitor_id}/?{query}",
                timeout=30,
            ) as response:
                payload = json.load(response)

            overlapping = [
                item
                for item in payload.get("videos", [])
                if parse_shinobi_time(item["time"]) < end_utc
                and parse_shinobi_time(item["end"]) > start_utc
            ]
            if not overlapping:
                raise RuntimeError(
                    f"No original recording overlaps the requested interval for {name}"
                )

            camera_dir = originals_dir / safe_slug(name)
            camera_dir.mkdir(parents=True, exist_ok=True)
            for item in sorted(overlapping, key=lambda value: value["time"]):
                filename = Path(item["filename"]).name
                output = camera_dir / filename
                download_jobs.append(
                    {
                        "camera": name,
                        "monitor_id": monitor_id,
                        "item": item,
                        "output": output,
                        "url": f"{base_url}{item['href']}",
                    }
                )

        downloads = download_jobs_parallel(
            download_jobs,
            args.download_workers,
        )

        manifest = {
            "station": args.station,
            "timezone": args.timezone,
            "requested_start_utc": start_utc.isoformat(),
            "requested_end_utc": end_utc.isoformat(),
            "requested_start_local": start_local.isoformat(),
            "requested_end_local": end_utc.astimezone(
                ZoneInfo(args.timezone)
            ).isoformat(),
            "originals": downloads,
        }
        args.output_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = args.output_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "manifest": str(manifest_path.resolve()),
                    "downloaded_originals": len(downloads),
                    "cameras": sorted({item["camera"] for item in downloads}),
                    "requested_start_local": manifest["requested_start_local"],
                    "requested_end_local": manifest["requested_end_local"],
                },
                indent=2,
            )
        )
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


if __name__ == "__main__":
    main()
