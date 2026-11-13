#!/usr/bin/env python3
"""Explicit-account, locked annotation jobs. No secrets in arguments or artifacts."""
import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time

import frontier_annotation as runner


def timestamp():
    return datetime.now().astimezone().isoformat()


def write_json(path, value):
    pending = path.with_suffix(path.suffix + ".pending")
    pending.write_text(json.dumps(value, indent=2) + "\n")
    pending.replace(path)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dotenv_key(path, variable):
    """Parse one literal assignment, never execute shell input."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
        raise ValueError("Invalid credential variable name")
    if path.stat().st_mode & 0o077:
        raise ValueError("Credential file must be owner-only")
    values = []
    for line in path.read_text().splitlines():
        match = re.match(rf"\s*(?:export\s+)?{re.escape(variable)}\s*=\s*(.*)$", line)
        if match:
            parts = shlex.split(match[1], comments=True)
            if len(parts) != 1:
                raise ValueError("Expected one literal credential value")
            values.append(parts[0])
    if len(values) != 1 or not values[0] or any(c in values[0] for c in "\n\r$`"):
        raise ValueError("Missing, duplicate, or nonliteral credential assignment")
    return values[0]


def inventory(images):
    paths = runner.image_paths(images)
    by_stem = {p.stem: p for p in paths}
    if not paths or len(paths) != len(by_stem):
        raise ValueError("Inputs must be nonempty with unique image stems")
    return by_stem


def validate_outputs(images, output, allowed):
    expected = set(inventory(images))
    labels = {p.stem: p for p in output.glob("*.txt")}
    crops = {p.name.removesuffix("_crop.png"): p for p in output.glob("*_crop.png")}
    return {
        "checked_at": timestamp(), "input_count": len(expected),
        "label_count": len(labels), "crop_count": len(crops),
        "missing_labels": sorted(expected - labels.keys()),
        "extra_labels": sorted(labels.keys() - expected),
        "missing_crops": sorted(expected - crops.keys()),
        "extra_crops": sorted(crops.keys() - expected),
        "invalid_labels": sorted(s for s, p in labels.items() if p.read_text().strip() not in allowed),
        "empty_crops": sorted(s for s, p in crops.items() if p.stat().st_size == 0),
    }


def complete(report):
    return not any(report[k] for k in (
        "missing_labels", "extra_labels", "missing_crops", "extra_crops",
        "invalid_labels", "empty_crops",
    ))


def seed_results(by_stem, output, config, sources, allowed):
    """Preflight every source before importing any result; never overwrite differences."""
    from PIL import Image
    pairs, provenance, planned = [], [], {}
    model = runner.parse_config(config)["model"]
    for root in sources:
        if digest(root / "product_annotation.yaml") != digest(config):
            raise ValueError("Calibration config differs")
        source_images = inventory(root / "images")
        source_labels = runner.output_dir(root / "images", model)
        for label in sorted(source_labels.glob("*.txt")):
            stem = label.stem
            if stem not in by_stem or stem not in source_images:
                raise ValueError("Calibration image not found in target")
            if digest(source_images[stem]) != digest(by_stem[stem]):
                raise ValueError("Calibration image content differs")
            if label.read_text().strip() not in allowed:
                raise ValueError("Invalid calibration label")
            crop = source_labels / f"{stem}_crop.png"
            with Image.open(crop) as image:
                image.verify()
            for source in (label, crop):
                target = output / source.name
                source_digest = digest(source)
                if target in planned and planned[target] != source_digest:
                    raise ValueError("Calibration sources disagree on an output")
                planned[target] = source_digest
                if target.exists() and digest(source) != digest(target):
                    raise ValueError("Existing output differs; refusing overwrite")
                pairs.append((source, target))
            provenance.append({"stem": stem, "source_job": str(root), "config_sha256": digest(config)})
    for source, target in pairs:
        if not target.exists():
            shutil.copy2(source, target)
    return provenance


def process_one(annotation, client, path, output):
    """Never evaluate response.output_text again while reporting a failure."""
    result = {"stem": path.stem, "started_at": timestamp()}
    response = None
    try:
        encoded, crop = annotation.encode_image_to_data_url(str(path))
        response = annotation.annotate_image(client, encoded)
        label = annotation.label_from_response(response)
        annotation.save_annotated_crop(crop, label, output / f"{path.stem}_crop.png")
        # A label is the durable completion marker, written after its crop.
        pending = output / f"{path.stem}.txt.pending"
        annotation.save_label(label, pending)
        pending.replace(output / f"{path.stem}.txt")
        result.update(success=True, label=label)
    except Exception as error:
        result.update(success=False, error_type=type(error).__name__,
                      http_status=getattr(error, "status_code", None))
    if response is not None:
        for name in ("id", "model", "status", "usage"):
            try:
                value = getattr(response, name, None)
                result[name] = value.model_dump() if hasattr(value, "model_dump") else value
            except Exception:
                result[name] = None
    result["finished_at"] = timestamp()
    return result


def run_job(args):
    images, config = args.images.resolve(), args.config.resolve()
    by_stem = inventory(images)
    parsed = runner.parse_config(config)
    output = runner.output_dir(images, parsed["model"])
    state_dir = output / ".job"
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        source = runner.discover(args.repo, runner.ANNOTATION_CANDIDATES, "annotation script")
        # Detect changed inputs/config/source/account on a resume.
        input_signature = hashlib.sha256(json.dumps([
            (str(p.relative_to(images)), p.stat().st_size, p.stat().st_mtime_ns)
            for p in by_stem.values()
        ]).encode()).hexdigest()
        identity = {
            "images": str(images), "output": str(output), "count": len(by_stem),
            "input_signature": input_signature, "config_sha256": digest(config),
            "source_sha256": digest(source), "model": parsed["model"],
            "credential_source": args.credential_source,
            "credential_profile": args.credential_profile, "base_url": args.base_url,
            "credential_file": str(args.credential_file.resolve()) if args.credential_file else None,
        }
        identity_path = state_dir / "identity.json"
        if identity_path.exists() and json.loads(identity_path.read_text()) != identity:
            raise ValueError("Job identity changed; use a separate approved output/input job")
        if not identity_path.exists():
            if list(output.glob("*.txt")) or list(output.glob("*_crop.png")):
                raise ValueError("Untracked outputs exist; import verified source jobs into a new job")
            write_json(identity_path, identity)
            shutil.copy2(config, state_dir / "product_annotation.yaml")
        if args.reuse_from:
            provenance = seed_results(by_stem, output, config, args.reuse_from, parsed["labels"])
            with (state_dir / "reuse.jsonl").open("a") as stream:
                for item in provenance:
                    stream.write(json.dumps(item) + "\n")
        report = validate_outputs(images, output, parsed["labels"])
        if any(report[k] for k in ("extra_labels", "extra_crops", "invalid_labels", "empty_crops")):
            raise ValueError("Invalid existing outputs; inspect before resuming")
        if set(report["missing_crops"]) - set(report["missing_labels"]):
            raise ValueError("Completed labels lack crops; repair locally before model calls")
        pending = [by_stem[s] for s in report["missing_labels"]]
        conservative_crop_bytes = sum(p.stat().st_size for p in pending)
        if shutil.disk_usage(output).free < conservative_crop_bytes:
            raise ValueError("Insufficient conservative crop disk reserve")
        staged = runner.stage_annotation_script(source, config, workers=args.workers,
                                                api_max_retries=args.api_max_retries)
        spec = importlib.util.spec_from_file_location("job_annotation", staged)
        annotation = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(annotation)
        from openai import OpenAI
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"], base_url=args.base_url,
                        max_retries=args.api_max_retries, timeout=120)
        stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f%z")
        state = {"started_at": timestamp(), "status": "running", "pid": os.getpid(),
                 "pending_at_start": len(pending), "created": 0, "failed": 0,
                 "workers": args.workers, "api_max_retries": args.api_max_retries}
        write_json(state_dir / "state.json", state)
        iterator = iter(pending)
        consecutive_failures = 0
        last_heartbeat = time.monotonic()
        with (state_dir / f"results-{stamp}.jsonl").open("x") as journal:
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                active = set()
                def submit():
                    path = next(iterator, None)
                    if path is not None:
                        active.add(pool.submit(process_one, annotation, client, path, output))
                for _ in range(args.workers):
                    submit()
                while active:
                    done, active = wait(active, timeout=1, return_when=FIRST_COMPLETED)
                    for future in done:
                        result = future.result()
                        journal.write(json.dumps(result) + "\n")
                        journal.flush()
                        state["created" if result["success"] else "failed"] += 1
                        consecutive_failures = 0 if result["success"] else consecutive_failures + 1
                        if consecutive_failures >= args.stop_after_failures:
                            state["status"] = "stopping-after-failures"
                        if state["status"] == "running":
                            submit()
                    state["checked_at"] = timestamp()
                    write_json(state_dir / "state.json", state)
                    if time.monotonic() - last_heartbeat >= args.progress_seconds:
                        print(json.dumps(state), flush=True)
                        last_heartbeat = time.monotonic()
        client.close()
        report = validate_outputs(images, output, parsed["labels"])
        write_json(state_dir / "validation.json", report)
        state.update(status="complete" if complete(report) else "incomplete",
                     finished_at=timestamp())
        write_json(state_dir / "state.json", state)
        print(json.dumps(state), flush=True)
        return 0 if complete(report) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=runner.DEFAULT_REPO)
    parser.add_argument("--credential-source", choices=["agent-secrets", "dotenv"], required=True)
    parser.add_argument("--credential-profile", required=True)
    parser.add_argument("--credential-file", type=Path)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--api-max-retries", type=int, default=5)
    parser.add_argument("--progress-seconds", type=int, default=60)
    parser.add_argument("--stop-after-failures", type=int, default=20)
    parser.add_argument("--reuse-from", type=Path, action="append", default=[])
    parser.add_argument("--approved-billable-run", action="store_true")
    parser.add_argument("--_injected", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.approved_billable_run:
        parser.error("Explicit billable approval is required")
    if not 1 <= args.workers <= 32 or not 0 <= args.api_max_retries <= 10:
        parser.error("Invalid worker/retry limits")
    if args.progress_seconds < 1 or args.stop_after_failures < 1:
        parser.error("Progress and failure limits must be positive")
    if not args.base_url.startswith("https://") or "@" in args.base_url:
        parser.error("Use an explicit HTTPS endpoint without embedded credentials")
    if args.credential_source == "dotenv" and not args.credential_file:
        parser.error("Explicit credential file required for dotenv")
    if args._injected:
        if not os.environ.get("OPENAI_API_KEY"):
            raise ValueError("Credential injection missing")
        return run_job(args)
    environment = os.environ.copy()
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID"):
        environment.pop(name, None)
    child = [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:], "--_injected"]
    if args.credential_source == "dotenv":
        environment["OPENAI_API_KEY"] = dotenv_key(args.credential_file, args.credential_profile)
    else:
        secrets = Path.home() / ".codex/skills/agent-secrets/scripts/agent_secrets.py"
        child = [sys.executable, str(secrets), "run", "--profile", args.credential_profile,
                 "--env", "OPENAI_API_KEY=private.api_key", "--", *child]
    return subprocess.run(child, env=environment, check=False).returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        # Avoid SDK messages or config contents in console diagnostics.
        print(f"Job stopped: {type(error).__name__}. Check job state and local inputs.", file=sys.stderr)
        raise SystemExit(2)
