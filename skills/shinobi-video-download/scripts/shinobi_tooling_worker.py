#!/usr/bin/env python3

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def safe_slug(value):
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_file(url, output):
    partial = output.with_suffix(output.suffix + ".part")
    with urllib.request.urlopen(url, timeout=180) as response, partial.open(
        "wb"
    ) as destination:
        shutil.copyfileobj(response, destination, length=1024 * 1024)
    os.replace(partial, output)


def run(command):
    subprocess.run(command, check=True)


def probe(path):
    output = subprocess.check_output(
        [
            "/usr/bin/ffprobe",
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


def summarize(path):
    details = probe(path)
    video_stream = next(
        (
            stream
            for stream in details.get("streams", [])
            if stream.get("codec_name")
        ),
        {},
    )
    file_format = details.get("format", {})
    return {
        "filename": path.name,
        "codec": video_stream.get("codec_name"),
        "width": video_stream.get("width"),
        "height": video_stream.get("height"),
        "duration_seconds": file_format.get("duration"),
        "bytes": file_format.get("size"),
        "sha256": sha256_file(path),
    }


def fetch_json(url, timeout=30):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


def select_monitors(request):
    base_url = request["base_url"].rstrip("/")
    monitors = fetch_json(
        f"{base_url}/{request['auth_token']}/monitor/{request['group_key']}"
    )
    if isinstance(monitors, dict):
        monitors = monitors.get("monitors", [])
    station = request["station"].lower()
    cameras = [camera.lower() for camera in request["cameras"]]
    selected = [
        monitor
        for monitor in monitors
        if station in str(monitor.get("name", "")).lower()
        and any(
            camera in str(monitor.get("name", "")).lower()
            for camera in cameras
        )
    ]
    if not selected:
        raise RuntimeError(
            f"No monitor matched station {request['station']!r} "
            f"and cameras {request['cameras']!r}"
        )
    return selected


def build_download_jobs(request, monitors, originals_dir):
    base_url = request["base_url"].rstrip("/")
    start = parse_time(request["start_utc"])
    end = parse_time(request["end_utc"])
    padding = timedelta(minutes=request["query_padding_minutes"])
    query = urllib.parse.urlencode(
        {
            "start": (start - padding).strftime("%Y-%m-%d %H:%M:%S"),
            "end": (end + padding).strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    jobs = []
    for monitor in monitors:
        name = monitor["name"]
        monitor_id = monitor["mid"]
        payload = fetch_json(
            f"{base_url}/{request['auth_token']}/videos/"
            f"{request['group_key']}/{monitor_id}/?{query}"
        )
        overlapping = [
            item
            for item in payload.get("videos", [])
            if parse_time(item["time"]) < end
            and parse_time(item["end"]) > start
        ]
        if not overlapping:
            raise RuntimeError(
                f"No original recording overlaps the requested interval for {name}"
            )
        camera_dir = originals_dir / safe_slug(name)
        camera_dir.mkdir(parents=True, exist_ok=True)
        for item in overlapping:
            jobs.append(
                {
                    "camera": name,
                    "monitor_id": monitor_id,
                    "item": item,
                    "output": camera_dir / Path(item["filename"]).name,
                    "url": f"{base_url}{item['href']}",
                }
            )
    return jobs


def download_original(job):
    item = job["item"]
    output = job["output"]
    download_file(job["url"], output)
    return {
        "camera": job["camera"],
        "monitor_id": job["monitor_id"],
        "recording_start_utc": item["time"],
        "recording_end_utc": item["end"],
        "path": str(output),
        "bytes": output.stat().st_size,
        "sha256": sha256_file(output),
    }


def download_originals(jobs, workers):
    downloads = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(download_original, job) for job in jobs]
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


def make_camera_clip(request, camera, segments, clips_dir, work_dir):
    start = parse_time(request["start_utc"])
    end = parse_time(request["end_utc"])
    parts = []
    for index, segment in enumerate(
        sorted(segments, key=lambda item: item["recording_start_utc"])
    ):
        segment_start = parse_time(segment["recording_start_utc"])
        segment_end = parse_time(segment["recording_end_utc"])
        clip_start = max(start, segment_start)
        clip_end = min(end, segment_end)
        if clip_end <= clip_start:
            continue
        part = work_dir / f"{safe_slug(camera)}-{index:02d}.mp4"
        run(
            [
                "/usr/bin/ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{(clip_start - segment_start).total_seconds():.3f}",
                "-i",
                segment["path"],
                "-t",
                f"{(clip_end - clip_start).total_seconds():.3f}",
                "-an",
                "-vf",
                f"fps={request['fps']}",
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

    output = clips_dir / (
        f"{safe_slug(camera)}-{start.strftime('%Y%m%dT%H%M%SZ')}-"
        f"{end.strftime('%Y%m%dT%H%M%SZ')}.mp4"
    )
    if len(parts) == 1:
        shutil.copy2(parts[0], output)
    else:
        concat_file = work_dir / f"{safe_slug(camera)}-concat.txt"
        concat_file.write_text(
            "".join(f"file '{part.as_posix()}'\n" for part in parts),
            encoding="utf-8",
        )
        run(
            [
                "/usr/bin/ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
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


def make_hstack(request, clips, clips_dir):
    start = parse_time(request["start_utc"])
    end = parse_time(request["end_utc"])
    output = clips_dir / (
        f"{safe_slug(request['station'])}-hstack-"
        f"{start.strftime('%Y%m%dT%H%M%SZ')}-"
        f"{end.strftime('%Y%m%dT%H%M%SZ')}.mp4"
    )
    filter_parts = [
        f"[{index}:v]scale=-2:{request['stack_height']}[v{index}]"
        for index in range(len(clips))
    ]
    inputs = "".join(f"[v{index}]" for index in range(len(clips)))
    filter_complex = (
        ";".join(filter_parts)
        + f";{inputs}hstack=inputs={len(clips)}[out]"
    )
    command = ["/usr/bin/ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
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
            str(output),
        ]
    )
    run(command)
    return output


def export_artifacts(request, manifest, videos, primary, work_dir):
    export_dir = work_dir / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    exported_videos = videos if request["layout"] == "separate" else [primary]
    video_summaries = []
    for video in exported_videos:
        destination = export_dir / video.name
        shutil.copy2(video, destination)
        video_summaries.append(summarize(destination))

    manifest_path = export_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    duration = float(summarize(primary)["duration_seconds"])
    qa_frames = []
    for index, percentage in enumerate((20, 50, 80), start=1):
        offset = duration * percentage / 100
        output = export_dir / f"frame-{index:02d}-{percentage:02d}pct.jpg"
        run(
            [
                "/usr/bin/ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{offset:.3f}",
                "-i",
                str(primary),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(output),
            ]
        )
        qa_frames.append(
            {
                "filename": output.name,
                "offset_seconds": round(offset, 3),
                "bytes": output.stat().st_size,
                "sha256": sha256_file(output),
            }
        )

    result = {
        "status": "ok",
        "primary_video": next(
            item for item in video_summaries if item["filename"] == primary.name
        ),
        "videos": video_summaries,
        "qa_frames": qa_frames,
        "manifest": manifest_path.name,
        "manifest_bytes": manifest_path.stat().st_size,
        "manifest_sha256": sha256_file(manifest_path),
    }
    (export_dir / "result.json").write_text(
        json.dumps(result, indent=2) + "\n",
        encoding="utf-8",
    )
    return result


def process(request):
    work_dir = Path(request["work_dir"])
    if (
        not work_dir.is_absolute()
        or work_dir.parent != Path("/tmp")
        or not work_dir.name.startswith("codex-shinobi-")
    ):
        raise RuntimeError("work_dir must be an isolated /tmp/codex-shinobi-* path")
    if request["download_workers"] < 1:
        raise RuntimeError("download_workers must be at least 1")
    if request["layout"] not in {"hstack", "separate"}:
        raise RuntimeError("layout must be hstack or separate")

    originals_dir = work_dir / "originals"
    clips_dir = work_dir / "clips"
    processing_dir = work_dir / "processing"
    for path in (originals_dir, clips_dir, processing_dir):
        path.mkdir(parents=True, exist_ok=True)

    monitors = select_monitors(request)
    jobs = build_download_jobs(request, monitors, originals_dir)
    originals = download_originals(jobs, request["download_workers"])
    by_camera = defaultdict(list)
    for item in originals:
        by_camera[item["camera"]].append(item)

    clips = [
        make_camera_clip(
            request,
            camera,
            segments,
            clips_dir,
            processing_dir,
        )
        for camera, segments in sorted(by_camera.items())
    ]
    if request["layout"] == "hstack":
        if len(clips) < 2:
            raise RuntimeError("hstack requires at least two cameras")
        primary = make_hstack(request, clips, clips_dir)
    else:
        primary = clips[0]

    manifest = {
        "station": request["station"],
        "timezone": request["timezone"],
        "requested_start_local": request["start_local"],
        "requested_end_local": request["end_local"],
        "requested_start_utc": request["start_utc"],
        "requested_end_utc": request["end_utc"],
        "cameras": sorted(by_camera),
        "originals": [
            {key: value for key, value in item.items() if key != "path"}
            for item in originals
        ],
    }
    return export_artifacts(
        request,
        manifest,
        clips,
        primary,
        work_dir,
    )


def main():
    request = json.load(sys.stdin)
    try:
        result = process(request)
    except Exception as exc:
        message = str(exc).replace(request.get("auth_token", ""), "<redacted>")
        print(json.dumps({"status": "error", "message": message}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
