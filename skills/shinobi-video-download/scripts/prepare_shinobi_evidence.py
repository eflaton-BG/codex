#!/usr/bin/env python3

import argparse
import json
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class SiteConfig:
    context: str
    timezone: str
    namespace: str = "dvr"
    pod: str = "shinobi-0"
    secret: str = "shinobi-secrets"


SITE_CONFIGS = {
    "pittston": SiteConfig(
        context="k8s/washington-pit-context",
        timezone="America/New_York",
    ),
}

SITE_ALIASES = {
    "pit": "pittston",
    "pittston": "pittston",
    "washington-pit": "pittston",
    "washington-pittston": "pittston",
}

CAMERA_ROLES = {
    "front": "Front CAM",
    "top": "Top CAM",
    "wing": "Wing CAM",
}

QA_FRAME_PERCENTAGES = (20, 50, 80)


class StageError(RuntimeError):
    def __init__(self, stage, message):
        super().__init__(message)
        self.stage = stage


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Download original Shinobi recordings and create local evidence clips."
        )
    )
    parser.add_argument("--site", required=True)
    parser.add_argument("--station", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument(
        "--camera",
        action="append",
        dest="cameras",
        help="Camera role (front/top/wing) or a monitor-name fragment; repeatable.",
    )
    parser.add_argument(
        "--layout",
        choices=("hstack", "separate"),
        default="hstack",
    )
    parser.add_argument(
        "--execution",
        choices=("auto", "deployment", "console", "local"),
        default="auto",
        help=(
            "Execution backend. auto uses tooling/console, then falls back to "
            "local execution if the console backend is unavailable."
        ),
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--query-padding-minutes", type=int, default=6)
    parser.add_argument("--download-workers", type=int, default=4)
    parser.add_argument(
        "--copy-retries",
        type=int,
        default=-1,
        help="kubectl cp retries for tooling artifacts; defaults to infinite.",
    )
    parser.add_argument("--stack-height", type=int, default=480)
    parser.add_argument("--fps", type=int, default=20)
    parser.add_argument("--local-port", type=int, default=18089)
    parser.add_argument("--kubectl", default="/usr/local/bin/kubectl")
    parser.add_argument("--ffmpeg", default="/usr/bin/ffmpeg")
    parser.add_argument("--ffprobe", default="/usr/bin/ffprobe")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the resolved plan and commands without creating files or connecting.",
    )
    return parser.parse_args()


def normalize(value):
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def resolve_site(site):
    site_key = SITE_ALIASES.get(normalize(site))
    if site_key is None:
        supported = ", ".join(sorted(SITE_CONFIGS))
        raise SystemExit(f"Unsupported site {site!r}; supported sites: {supported}")
    return site_key, SITE_CONFIGS[site_key]


def parse_time(value, timezone_name):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
    return parsed.astimezone(timezone.utc)


def videos_root():
    return (Path.home() / "Videos").resolve()


def resolve_output_dir(output_dir, station, start_utc, timezone_name):
    root = videos_root()
    if output_dir is None:
        start_local = start_utc.astimezone(ZoneInfo(timezone_name))
        label = start_local.strftime("%Y%m%dT%H%M%S")
        output_dir = root / "Shinobi" / f"{normalize(station)}-{label}"
    else:
        output_dir = output_dir.expanduser().resolve()
    try:
        output_dir.relative_to(root)
    except ValueError as exc:
        raise SystemExit(
            f"Video output must be beneath the user's Videos directory: {root}"
        ) from exc
    return output_dir


def resolve_cameras(cameras):
    requested = cameras or ["front", "top"]
    return [CAMERA_ROLES.get(normalize(camera), camera) for camera in requested]


def sanitized(text):
    return re.sub(
        r"https?://127\.0\.0\.1:\d+/[^\s'\"\\]+",
        "http://127.0.0.1:<port>/<redacted>",
        text,
    )


def append_log(log_path, stage, command, result):
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(f"\n[{stage}]\n$ {shlex.join(command)}\n")
        if result.stdout:
            stream.write(sanitized(result.stdout))
            if not result.stdout.endswith("\n"):
                stream.write("\n")
        if result.stderr:
            stream.write(sanitized(result.stderr))
            if not result.stderr.endswith("\n"):
                stream.write("\n")


def run_command(command, stage, log_path, expect_json=False):
    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
    )
    append_log(log_path, stage, command, result)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        last_line = sanitized(detail).splitlines()[-1] if detail else "command failed"
        raise StageError(stage, last_line)
    if not expect_json:
        return result.stdout.strip()
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise StageError(stage, "command did not return valid JSON") from exc


def build_commands(
    args,
    site,
    config,
    cameras,
    output_dir,
    scripts_dir,
    start_utc,
    end_utc,
):
    manifest_path = output_dir / "manifest.json"
    preflight = [
        args.kubectl,
        "get",
        "pod",
        config.pod,
        "-n",
        config.namespace,
        "-o",
        "name",
        "--context",
        config.context,
    ]
    download = [
        sys.executable,
        str(scripts_dir / "download_originals.py"),
        "--context",
        config.context,
        "--station",
        args.station,
        "--start",
        args.start,
        "--end",
        args.end,
        "--timezone",
        config.timezone,
        "--output-dir",
        str(output_dir),
        "--namespace",
        config.namespace,
        "--pod",
        config.pod,
        "--secret",
        config.secret,
        "--local-port",
        str(args.local_port),
        "--query-padding-minutes",
        str(args.query_padding_minutes),
        "--download-workers",
        str(args.download_workers),
        "--kubectl",
        args.kubectl,
    ]
    for camera in cameras:
        download.extend(["--camera", camera])

    clip = [
        sys.executable,
        str(scripts_dir / "make_local_clip.py"),
        "--manifest",
        str(manifest_path),
        "--output-dir",
        str(output_dir / "clips"),
        "--stack-height",
        str(args.stack_height),
        "--fps",
        str(args.fps),
        "--ffmpeg",
        args.ffmpeg,
        "--ffprobe",
        args.ffprobe,
    ]
    if args.layout == "hstack":
        clip.append("--hstack")

    tooling = [
        sys.executable,
        str(scripts_dir / "run_tooling_backend.py"),
        "--execution",
        "auto" if args.execution == "auto" else args.execution,
        "--context",
        config.context,
        "--station",
        args.station,
        "--start-local",
        start_utc.astimezone(ZoneInfo(config.timezone)).isoformat(),
        "--end-local",
        end_utc.astimezone(ZoneInfo(config.timezone)).isoformat(),
        "--start-utc",
        start_utc.isoformat().replace("+00:00", "Z"),
        "--end-utc",
        end_utc.isoformat().replace("+00:00", "Z"),
        "--timezone",
        config.timezone,
        "--layout",
        args.layout,
        "--output-dir",
        str(output_dir),
        "--query-padding-minutes",
        str(args.query_padding_minutes),
        "--download-workers",
        str(args.download_workers),
        "--copy-retries",
        str(args.copy_retries),
        "--stack-height",
        str(args.stack_height),
        "--fps",
        str(args.fps),
        "--local-port",
        str(args.local_port),
        "--kubectl",
        args.kubectl,
        "--shinobi-namespace",
        config.namespace,
        "--shinobi-pod",
        config.pod,
        "--shinobi-secret",
        config.secret,
    ]
    for camera in cameras:
        tooling.extend(["--camera", camera])

    return {
        "site": site,
        "context": config.context,
        "timezone": config.timezone,
        "station": args.station,
        "start": args.start,
        "end": args.end,
        "cameras": cameras,
        "layout": args.layout,
        "execution": args.execution,
        "output_dir": str(output_dir),
        "qa_frame_percentages": QA_FRAME_PERCENTAGES,
        "commands": {
            "preflight": preflight,
            "tooling": tooling,
            "download": download,
            "clip": clip,
        },
    }


def summarize_output(item):
    probe = item.get("probe", {})
    video_stream = next(
        (
            stream
            for stream in probe.get("streams", [])
            if stream.get("codec_name")
        ),
        {},
    )
    file_format = probe.get("format", {})
    return {
        "path": item["path"],
        "codec": video_stream.get("codec_name"),
        "width": video_stream.get("width"),
        "height": video_stream.get("height"),
        "duration_seconds": file_format.get("duration"),
        "bytes": file_format.get("size"),
    }


def make_qa_frames(args, primary, output_dir, log_path):
    try:
        duration = float(primary["duration_seconds"])
    except (TypeError, ValueError) as exc:
        raise StageError(
            "result-validation",
            "primary video did not report a valid duration",
        ) from exc

    qa_dir = output_dir / "qa"
    qa_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for index, percentage in enumerate(QA_FRAME_PERCENTAGES, start=1):
        offset = duration * percentage / 100
        path = qa_dir / f"frame-{index:02d}-{percentage:02d}pct.jpg"
        command = [
            args.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{offset:.3f}",
            "-i",
            primary["path"],
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(path),
        ]
        run_command(
            command,
            f"qa-frame-{percentage:02d}pct",
            log_path,
        )
        frames.append(
            {
                "path": str(path.resolve()),
                "offset_seconds": round(offset, 3),
            }
        )
    return frames


def run_tooling(plan, args, site, log_path):
    result = subprocess.run(
        plan["commands"]["tooling"],
        text=True,
        capture_output=True,
        check=False,
    )
    append_log(log_path, "tooling-backend", plan["commands"]["tooling"], result)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = None
    if result.returncode == 0 and payload:
        payload["site"] = site
        payload["log"] = str(log_path)
        print(json.dumps(payload, indent=2))
        return 0
    if result.returncode == 2 and args.execution == "auto":
        return None

    message = None
    stage = "tooling-backend"
    if payload:
        message = payload.get("message")
        stage = payload.get("stage", stage)
    if not message:
        detail = result.stderr.strip() or result.stdout.strip()
        message = sanitized(detail).splitlines()[-1] if detail else "command failed"
    print(
        json.dumps(
            {
                "status": "error",
                "stage": stage,
                "message": message,
                "log": str(log_path),
            },
            indent=2,
        )
    )
    return 1


def main():
    args = parse_args()
    site, config = resolve_site(args.site)
    start_utc = parse_time(args.start, config.timezone)
    end_utc = parse_time(args.end, config.timezone)
    if end_utc <= start_utc:
        raise SystemExit("--end must be after --start")

    cameras = resolve_cameras(args.cameras)
    if args.layout == "hstack" and len(cameras) < 2:
        raise SystemExit("--layout hstack requires at least two cameras")

    output_dir = resolve_output_dir(
        args.output_dir,
        args.station,
        start_utc,
        config.timezone,
    )
    scripts_dir = Path(__file__).resolve().parent
    plan = build_commands(
        args,
        site,
        config,
        cameras,
        output_dir,
        scripts_dir,
        start_utc,
        end_utc,
    )
    if args.dry_run:
        print(json.dumps({"status": "dry-run", "plan": plan}, indent=2))
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "prepare.log"
    log_path.write_text("", encoding="utf-8")

    if args.execution != "local":
        tooling_result = run_tooling(plan, args, site, log_path)
        if tooling_result is not None:
            return tooling_result

    try:
        run_command(
            plan["commands"]["preflight"],
            "kubernetes-preflight",
            log_path,
        )
        download_result = run_command(
            plan["commands"]["download"],
            "download-originals",
            log_path,
            expect_json=True,
        )
        manifest_path = Path(download_result["manifest"]).resolve()
        plan["commands"]["clip"][
            plan["commands"]["clip"].index("--manifest") + 1
        ] = str(manifest_path)
        clip_result = run_command(
            plan["commands"]["clip"],
            "make-local-clips",
            log_path,
            expect_json=True,
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        originals = manifest["originals"]
        raw_outputs = clip_result["outputs"]
        if not originals:
            raise StageError("result-validation", "manifest contains no originals")
        if not raw_outputs:
            raise StageError("result-validation", "clip script returned no outputs")
        outputs = [summarize_output(item) for item in raw_outputs]
        primary = next(
            (
                item
                for item in outputs
                if args.layout == "hstack"
                and "-hstack-" in Path(item["path"]).name
            ),
            outputs[0],
        )
        qa_frames = make_qa_frames(args, primary, output_dir, log_path)
    except (json.JSONDecodeError, KeyError, OSError, StageError) as exc:
        stage = exc.stage if isinstance(exc, StageError) else "result-validation"
        print(
            json.dumps(
                {
                    "status": "error",
                    "stage": stage,
                    "message": str(exc),
                    "log": str(log_path),
                },
                indent=2,
            )
        )
        return 1

    print(
        json.dumps(
            {
                "status": "ok",
                "execution": "local",
                "site": site,
                "station": args.station,
                "context": config.context,
                "timezone": config.timezone,
                "requested_start_local": manifest["requested_start_local"],
                "requested_end_local": manifest["requested_end_local"],
                "requested_start_utc": manifest["requested_start_utc"],
                "requested_end_utc": manifest["requested_end_utc"],
                "cameras": sorted(
                    {item["camera"] for item in originals}
                ),
                "originals": len(originals),
                "manifest": str(manifest_path),
                "primary_video": primary,
                "videos": outputs,
                "qa_frames": qa_frames,
                "log": str(log_path),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
