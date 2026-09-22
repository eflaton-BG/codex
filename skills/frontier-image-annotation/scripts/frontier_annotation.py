#!/usr/bin/env python3
"""Inspect and launch bg_ml frontier-model image annotation jobs safely."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from collections import Counter


DEFAULT_REPO = Path.home() / "devel/bg_ml_ws/bg_ml"
DEFAULT_WORKSPACE_PYTHON = (
    Path.home() / "devel/colcon_ws/src/.venv/bin/python"
)
EXPERIMENT_DIR = Path("experiments/robot_eligibility_classifier")
ANNOTATION_CANDIDATES = (
    EXPERIMENT_DIR / "scripts/annotation/openai_product_annotation.py",
    EXPERIMENT_DIR / "openai_product_annotation.py",
)
BUNDLED_DOWNLOADER = Path(__file__).with_name("download_s3_images.py")
DEFAULT_CONFIG = EXPERIMENT_DIR / "configs/product_annotation.yaml"


def absolute_without_resolving(path: Path) -> Path:
    """Return an absolute path while preserving virtualenv symlinks."""
    return Path(os.path.abspath(path.expanduser()))


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        check=check,
        text=True,
        capture_output=True,
    )


def discover(repo: Path, candidates: tuple[Path, ...], kind: str) -> Path:
    for candidate in candidates:
        path = repo / candidate
        if path.is_file():
            return path
    checked = ", ".join(str(repo / candidate) for candidate in candidates)
    raise FileNotFoundError(f"Could not find {kind}; checked: {checked}")


def git_state(repo: Path) -> dict[str, object]:
    if not (repo / ".git").exists():
        return {"is_git_repo": False}

    branch = run(
        ["/usr/bin/git", "-C", str(repo), "branch", "--show-current"]
    ).stdout.strip()
    status_lines = run(
        ["/usr/bin/git", "-C", str(repo), "status", "--short"]
    ).stdout.splitlines()
    return {
        "is_git_repo": True,
        "branch": branch,
        "dirty": bool(status_lines),
        "status": status_lines,
    }


def parse_config(config: Path) -> dict[str, object]:
    text = config.read_text(encoding="utf-8")
    model_match = re.search(r"(?m)^model:\s*[\"']?([^\"'\n#]+)", text)
    output_match = re.search(r"(?m)^\s+field:\s*[\"']?([^\"'\n#]+)", text)

    in_labels = False
    labels: list[str] = []
    for line in text.splitlines():
        if re.match(r"^labels:\s*$", line):
            in_labels = True
            continue
        if in_labels and re.match(r"^[A-Za-z_][\w-]*:\s*", line):
            break
        match = re.match(r"^\s{2}-\s+name:\s*(.+?)\s*$", line)
        if in_labels and match:
            labels.append(match.group(1).strip("\"'"))

    if not model_match:
        raise ValueError(f"Could not identify `model` in {config}")
    if not labels:
        raise ValueError(f"Could not identify any labels in {config}")

    return {
        "model": model_match.group(1).strip(),
        "labels": labels,
        "output_field": output_match.group(1).strip() if output_match else None,
    }


def dependency_state(python: Path, modules: list[str]) -> dict[str, object]:
    expression = "; ".join(f"import {module}" for module in modules)
    result = run([str(python), "-c", expression], check=False)
    return {
        "python": str(python),
        "modules": modules,
        "ok": result.returncode == 0,
        "error": result.stderr.strip() if result.returncode else None,
    }


def image_paths(images: Path) -> list[Path]:
    return sorted(
        path
        for path in images.rglob("*")
        if path.is_file() and path.suffix.lower() == ".png"
    )


def output_dir(images: Path, model: str) -> Path:
    return Path(f"{images}_labels_{model.replace('/', '_')}")


def resolve_config(repo: Path, value: Path | None) -> Path:
    config = value or repo / DEFAULT_CONFIG
    config = config.expanduser().resolve()
    if not config.is_file():
        raise FileNotFoundError(f"Config does not exist: {config}")
    return config


def inspect_job(args: argparse.Namespace) -> dict[str, object]:
    repo = args.repo.expanduser().resolve()
    images = args.images.expanduser().resolve()
    python = absolute_without_resolving(args.python)
    if not 1 <= args.workers <= 32:
        raise ValueError("--workers must be between 1 and 32")
    if not 0 <= args.api_max_retries <= 10:
        raise ValueError("--api-max-retries must be between 0 and 10")
    if not repo.is_dir():
        raise FileNotFoundError(f"Repository does not exist: {repo}")
    if not images.is_dir():
        raise FileNotFoundError(f"Image directory does not exist: {images}")

    config = resolve_config(repo, args.config)
    annotation_script = discover(
        repo, ANNOTATION_CANDIDATES, "annotation script"
    )
    config_data = parse_config(config)
    paths = image_paths(images)
    labels_dir = output_dir(images, str(config_data["model"]))
    existing = (
        sum(1 for path in paths if (labels_dir / f"{path.stem}.txt").is_file())
        if labels_dir.is_dir()
        else 0
    )

    return {
        "repo": str(repo),
        "git": git_state(repo),
        "annotation_script": str(annotation_script),
        "config": str(config),
        "model": config_data["model"],
        "label_count": len(config_data["labels"]),
        "output_field": config_data["output_field"],
        "images": str(images),
        "image_count": len(paths),
        "output_dir": str(labels_dir),
        "existing_label_count": existing,
        "remaining_billable_calls_up_to": max(len(paths) - existing, 0),
        "workers": args.workers,
        "api_max_retries": args.api_max_retries,
        "dependencies": dependency_state(python, ["openai", "PIL", "yaml"]),
        "credential_presence": {
            "OPENAI_API_KEY": bool(os.environ.get("OPENAI_API_KEY")),
            "OPENAI_BASE_URL": bool(os.environ.get("OPENAI_BASE_URL")),
        },
        "billable_run_started": False,
    }


def stage_annotation_script(
    source: Path,
    config: Path,
    *,
    workers: int,
    api_max_retries: int,
) -> Path:
    digest = hashlib.sha256()
    digest.update(source.read_bytes())
    digest.update(config.read_bytes())
    digest.update(f"workers={workers};api_max_retries={api_max_retries}".encode())
    stage_dir = Path("/tmp/frontier-image-annotation") / digest.hexdigest()[:16]
    configs_dir = stage_dir / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    staged_script = stage_dir / source.name
    staged_source = source.read_text(encoding="utf-8")
    staged_source, worker_replacements = re.subn(
        r"(?m)^MAX_WORKERS\s*=\s*\d+\s*$",
        f"MAX_WORKERS = {workers}",
        staged_source,
        count=1,
    )
    if worker_replacements != 1:
        raise ValueError(f"Could not configure MAX_WORKERS in {source}")

    client_expression = "client = OpenAI(timeout=REQUEST_TIMEOUT_SECONDS)"
    if client_expression not in staged_source:
        raise ValueError(f"Could not configure OpenAI retries in {source}")
    staged_source = staged_source.replace(
        client_expression,
        (
            "client = OpenAI("
            "timeout=REQUEST_TIMEOUT_SECONDS, "
            f"max_retries={api_max_retries}"
            ")"
        ),
        1,
    )
    staged_script.write_text(staged_source, encoding="utf-8")
    shutil.copymode(source, staged_script)
    for filename in ("product_annotation.yaml", "product_annotation_multiclass.yaml"):
        shutil.copy2(config, configs_dir / filename)
    return staged_script


def annotate(args: argparse.Namespace) -> int:
    plan = inspect_job(args)
    print(json.dumps(plan, indent=2))

    if not args.approved_billable_run:
        raise PermissionError(
            "Refusing to start API calls without --approved-billable-run"
        )
    if plan["image_count"] == 0:
        raise ValueError(f"No PNG images found in {plan['images']}")
    if not plan["dependencies"]["ok"]:
        raise RuntimeError("Required annotation dependencies are unavailable")
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not available in the environment")

    source = Path(str(plan["annotation_script"]))
    config = Path(str(plan["config"]))
    staged_script = stage_annotation_script(
        source,
        config,
        workers=args.workers,
        api_max_retries=args.api_max_retries,
    )
    command = [
        str(absolute_without_resolving(args.python)),
        str(staged_script),
        "--input-images-dir",
        str(args.images.expanduser().resolve()),
    ]
    if args.save_crops:
        command.append("--save-crops")

    environment = os.environ.copy()
    environment["PYTHONUNBUFFERED"] = "1"
    result = subprocess.run(command, check=False, env=environment)
    return result.returncode


def download(args: argparse.Namespace) -> int:
    input_csv = args.input_csv.expanduser().resolve()
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else input_csv.parent
        / (args.output_dir_name or f"{input_csv.stem}_images")
    )
    command_name = "download" if args.approved_download else "plan"
    command = [
        str(absolute_without_resolving(args.python)),
        str(BUNDLED_DOWNLOADER),
        command_name,
        "--input-csv",
        str(input_csv),
        "--mapping-csv",
        str(args.mapping_csv.expanduser().resolve()),
        "--output-dir",
        str(output_dir),
        "--workers",
        str(args.workers),
    ]
    if args.approved_download:
        command.append("--approved-download")
        if args.plan_id:
            command.extend(["--plan-id", args.plan_id])
    result = subprocess.run(command, check=False)
    return result.returncode


def annotation_status(args: argparse.Namespace) -> dict[str, object]:
    repo = args.repo.expanduser().resolve()
    images = args.images.expanduser().resolve()
    if not images.is_dir():
        raise FileNotFoundError(f"Image directory does not exist: {images}")

    config = resolve_config(repo, args.config)
    config_data = parse_config(config)
    inputs = image_paths(images)
    input_stems = {path.stem for path in inputs}
    labels_dir = output_dir(images, str(config_data["model"]))
    label_paths = (
        sorted(
            path
            for path in labels_dir.glob("*.txt")
            if path.is_file() and path.stem in input_stems
        )
        if labels_dir.is_dir()
        else []
    )
    crop_count = (
        sum(
            1
            for path in labels_dir.glob("*_crop.png")
            if path.is_file()
        )
        if labels_dir.is_dir()
        else 0
    )
    latest = max(label_paths, key=lambda path: path.stat().st_mtime, default=None)
    completed = len(label_paths)
    total = len(inputs)

    return {
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "images": str(images),
        "config": str(config),
        "model": config_data["model"],
        "output_dir": str(labels_dir),
        "total_images": total,
        "completed_labels": completed,
        "remaining_labels": max(total - completed, 0),
        "progress_percent": round(100 * completed / total, 2) if total else 0.0,
        "saved_crops": crop_count,
        "latest_label": str(latest) if latest else None,
        "latest_label_mtime": (
            datetime.fromtimestamp(latest.stat().st_mtime)
            .astimezone()
            .isoformat(timespec="seconds")
            if latest
            else None
        ),
    }


def read_labels(directory: Path) -> dict[str, str]:
    if not directory.is_dir():
        raise FileNotFoundError(f"Label directory does not exist: {directory}")
    return {
        path.stem: path.read_text(encoding="utf-8").strip()
        for path in sorted(directory.glob("*.txt"))
        if path.is_file()
    }


def evaluate(args: argparse.Namespace) -> dict[str, object]:
    gold_dir = args.gold_labels.expanduser().resolve()
    predicted_dir = args.predicted_labels.expanduser().resolve()
    gold = read_labels(gold_dir)
    predicted = read_labels(predicted_dir)
    matched_stems = sorted(gold.keys() & predicted.keys())
    correct = sum(gold[stem] == predicted[stem] for stem in matched_stems)
    confusion = Counter(
        (gold[stem], predicted[stem])
        for stem in matched_stems
        if gold[stem] != predicted[stem]
    )

    return {
        "gold_labels": str(gold_dir),
        "predicted_labels": str(predicted_dir),
        "gold_count": len(gold),
        "predicted_count": len(predicted),
        "matched_count": len(matched_stems),
        "correct_count": correct,
        "accuracy": correct / len(matched_stems) if matched_stems else None,
        "missing_predictions": sorted(gold.keys() - predicted.keys()),
        "unexpected_predictions": sorted(predicted.keys() - gold.keys()),
        "confusion": [
            {"gold": pair[0], "predicted": pair[1], "count": count}
            for pair, count in confusion.most_common()
        ],
    }


def add_common_job_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    parser.add_argument(
        "--python",
        type=Path,
        default=(
            DEFAULT_WORKSPACE_PYTHON
            if DEFAULT_WORKSPACE_PYTHON.is_file()
            else Path(sys.executable)
        ),
    )
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--workers",
        type=int,
        default=2,
        help="Concurrent annotation requests (1-32).",
    )
    parser.add_argument(
        "--api-max-retries",
        type=int,
        default=5,
        help="OpenAI SDK retries per request (0-10).",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser(
        "inspect", help="Inspect an annotation job without API calls."
    )
    add_common_job_arguments(inspect_parser)

    annotate_parser = subparsers.add_parser(
        "annotate", help="Run the selected bg_ml annotation script."
    )
    add_common_job_arguments(annotate_parser)
    annotate_parser.add_argument("--save-crops", action="store_true")
    annotate_parser.add_argument(
        "--approved-billable-run",
        action="store_true",
        help="Required only after explicit user approval.",
    )

    status_parser = subparsers.add_parser(
        "status", help="Report durable annotation progress without API calls."
    )
    status_parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    status_parser.add_argument("--images", type=Path, required=True)
    status_parser.add_argument("--config", type=Path)

    download_parser = subparsers.add_parser(
        "download", help="Inspect or execute the bg_ml S3 image downloader."
    )
    download_parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    download_parser.add_argument(
        "--python",
        type=Path,
        default=(
            DEFAULT_WORKSPACE_PYTHON
            if DEFAULT_WORKSPACE_PYTHON.is_file()
            else Path(sys.executable)
        ),
    )
    download_parser.add_argument("--input-csv", type=Path, required=True)
    download_parser.add_argument("--mapping-csv", type=Path, required=True)
    download_parser.add_argument("--output-dir", type=Path)
    download_parser.add_argument("--output-dir-name")
    download_parser.add_argument("--workers", type=int, default=16)
    download_parser.add_argument("--plan-id")
    download_parser.add_argument("--approved-download", action="store_true")

    evaluate_parser = subparsers.add_parser(
        "evaluate", help="Compare generated text labels with hand labels."
    )
    evaluate_parser.add_argument("--gold-labels", type=Path, required=True)
    evaluate_parser.add_argument("--predicted-labels", type=Path, required=True)

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "inspect":
        print(json.dumps(inspect_job(args), indent=2))
        return 0
    if args.command == "annotate":
        return annotate(args)
    if args.command == "status":
        print(json.dumps(annotation_status(args), indent=2))
        return 0
    if args.command == "download":
        return download(args)
    if args.command == "evaluate":
        print(json.dumps(evaluate(args), indent=2))
        return 0
    raise AssertionError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2)
