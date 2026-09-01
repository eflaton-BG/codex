#!/usr/bin/env python3

import argparse
import json
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(
        description="Create clips from locally downloaded Shinobi originals."
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--camera", action="append", dest="cameras")
    parser.add_argument("--hstack", action="store_true")
    parser.add_argument("--stack-height", type=int, default=480)
    parser.add_argument("--fps", type=int, default=20)
    parser.add_argument("--ffmpeg", default="/usr/bin/ffmpeg")
    parser.add_argument("--ffprobe", default="/usr/bin/ffprobe")
    return parser.parse_args()


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def safe_slug(value):
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def videos_root():
    return (Path.home() / "Videos").resolve()


def require_video_path(path):
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(videos_root())
    except ValueError as exc:
        raise SystemExit(
            f"Video paths must be beneath the user's Videos directory: {videos_root()}"
        ) from exc
    return resolved


def run(command):
    subprocess.run(command, check=True)


def validate(ffprobe, path):
    output = subprocess.check_output(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration,size",
            "-show_entries",
            "stream=codec_name,width,height,r_frame_rate",
            "-of",
            "json",
            str(path),
        ],
        text=True,
    )
    return json.loads(output)


def make_camera_clip(args, camera, segments, start, end, work_dir):
    parts = []
    for index, segment in enumerate(sorted(segments, key=lambda item: item["recording_start_utc"])):
        segment_start = parse_time(segment["recording_start_utc"])
        segment_end = parse_time(segment["recording_end_utc"])
        clip_start = max(start, segment_start)
        clip_end = min(end, segment_end)
        if clip_end <= clip_start:
            continue
        offset = (clip_start - segment_start).total_seconds()
        duration = (clip_end - clip_start).total_seconds()
        part = work_dir / f"{safe_slug(camera)}-{index:02d}.mp4"
        run(
            [
                args.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{offset:.3f}",
                "-i",
                segment["path"],
                "-t",
                f"{duration:.3f}",
                "-an",
                "-vf",
                f"fps={args.fps}",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-movflags",
                "+faststart",
                str(part),
            ]
        )
        parts.append(part)
    if not parts:
        raise RuntimeError(f"No local segment overlap found for {camera}")

    start_label = start.strftime("%Y%m%dT%H%M%SZ")
    end_label = end.strftime("%Y%m%dT%H%M%SZ")
    output = args.output_dir / f"{safe_slug(camera)}-{start_label}-{end_label}.mp4"
    if len(parts) == 1:
        shutil.copy2(parts[0], output)
    else:
        concat_file = work_dir / f"{safe_slug(camera)}-concat.txt"
        concat_file.write_text(
            "".join(f"file '{part.as_posix()}'\n" for part in parts)
        )
        run(
            [
                args.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_file),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(output),
            ]
        )
    return output


def main():
    args = parse_args()
    args.manifest = args.manifest.expanduser().resolve()
    manifest = json.loads(args.manifest.read_text())
    args.output_dir = require_video_path(
        args.output_dir if args.output_dir else args.manifest.parent / "clips"
    )
    start = parse_time(manifest["requested_start_utc"])
    end = parse_time(manifest["requested_end_utc"])
    by_camera = defaultdict(list)
    for item in manifest["originals"]:
        item["path"] = str(require_video_path(Path(item["path"])))
        if args.cameras and not any(
            pattern.lower() in item["camera"].lower() for pattern in args.cameras
        ):
            continue
        by_camera[item["camera"]].append(item)
    if not by_camera:
        raise SystemExit("No manifest cameras matched the requested filter")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="shinobi-local-processing-", dir=args.output_dir
    ) as tmp:
        work_dir = Path(tmp)
        clips = [
            make_camera_clip(args, camera, segments, start, end, work_dir)
            for camera, segments in sorted(by_camera.items())
        ]

    outputs = list(clips)
    if args.hstack:
        if len(clips) < 2:
            raise SystemExit("--hstack requires at least two selected cameras")
        filter_parts = [
            f"[{index}:v]scale=-2:{args.stack_height}[v{index}]"
            for index in range(len(clips))
        ]
        inputs = "".join(f"[v{index}]" for index in range(len(clips)))
        filter_complex = (
            ";".join(filter_parts)
            + f";{inputs}hstack=inputs={len(clips)}[out]"
        )
        combined = args.output_dir / (
            f"{safe_slug(manifest['station'])}-hstack-"
            f"{start.strftime('%Y%m%dT%H%M%SZ')}-"
            f"{end.strftime('%Y%m%dT%H%M%SZ')}.mp4"
        )
        command = [args.ffmpeg, "-hide_banner", "-loglevel", "error"]
        for clip in clips:
            command.extend(["-i", str(clip)])
        command.extend(
            [
                "-filter_complex",
                filter_complex,
                "-map",
                "[out]",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-movflags",
                "+faststart",
                str(combined),
            ]
        )
        run(command)
        outputs.append(combined)

    print(
        json.dumps(
            {
                "outputs": [
                    {
                        "path": str(path.resolve()),
                        "probe": validate(args.ffprobe, path),
                    }
                    for path in outputs
                ]
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
