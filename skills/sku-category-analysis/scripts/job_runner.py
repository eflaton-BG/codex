"""Reusable locked local/S3 category jobs. Inputs/config/account belong to the job."""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
import csv
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import unquote, urlsplit

from openai import OpenAI
from PIL import Image
import yaml

import annotation_job as credentials
import checks
import job_runtime as rt

FORMAT = "sku-category-analysis/v1"
IMPLEMENTATION = ("run_job.sh", "job_runner.py", "job_runtime.py", "job_monitor.py", "job_control.py")
SCRIPT_DIR = Path(__file__).resolve().parent
TERMINAL = {"complete", "stopped", "stopped_error"}


def read(path):
    return json.loads(Path(path).read_text())


@contextmanager
def job_lock(root):
    with (root / ".job/run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def config_data(path):
    config = yaml.safe_load(path.read_text())
    if not isinstance(config, dict) or not {
        "name", "model", "task", "general_rules", "labels", "output"
    } <= config.keys():
        raise ValueError("Category YAML must use the BG task/rules/labels/output contract")
    labels = config["labels"]
    if not labels or any(not label.get("name") or not label.get("definition") for label in labels):
        raise ValueError("Every category requires a name and definition")
    if len({label["name"] for label in labels}) != len(labels):
        raise ValueError("Duplicate category names")
    if not config.get("model") or not config["output"].get("field"):
        raise ValueError("Model and output field must be explicit")
    return config


def inputs_from(images=None, manifest=None):
    """Deduplicate source images while retaining all associated SKU identifiers."""
    grouped = {}
    if images is not None:
        rows = [{"path": str(path)} for path in sorted(images.rglob("*"))
                if path.is_file() and path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")]
    else:
        with manifest.open(newline="") as stream:
            rows = list(csv.DictReader(stream))
    for row in rows:
        uri = row.get("s3_uri") or row.get("path")
        if not uri:
            raise ValueError("Manifest needs s3_uri or path for each row")
        if uri.startswith("s3://"):
            parsed = urlsplit(uri)
            size = int(row.get("s3_size_bytes") or row.get("size_bytes") or 0)
            etag = row.get("s3_etag") or row.get("etag")
            if not parsed.netloc or not parsed.path.strip("/") or parsed.query or parsed.fragment:
                raise ValueError("Invalid S3 URI")
            if size <= 0 or not etag:
                raise ValueError("S3 inputs require verified positive size and pinned ETag")
            item = {"uri": uri, "kind": "s3", "bucket": parsed.netloc, "key": parsed.path[1:],
                    "source_bytes": size, "source_etag": etag}
        else:
            path = Path(unquote(urlsplit(uri).path) if uri.startswith("file://") else uri).expanduser()
            if not path.is_absolute():
                path = manifest.parent / path
            path = path.resolve(strict=True)
            item = {"uri": path.as_uri(), "kind": "local", "path": str(path),
                    "source_bytes": path.stat().st_size, "source_sha256": rt.digest(path)}
            uri = item["uri"]
        sku = row.get("SkuId") or row.get("sku") or ""
        if uri in grouped:
            previous = {key: value for key, value in grouped[uri].items() if key not in ("skus", "index")}
            if previous != item:
                raise ValueError("Duplicate source rows disagree about pinned metadata")
        else:
            grouped[uri] = dict(item, skus=[], index=len(grouped) + 1)
        if sku and sku not in grouped[uri]["skus"]:
            grouped[uri]["skus"].append(sku)
    if not grouped:
        raise ValueError("No images found")
    return list(grouped.values())


def implementation_hashes():
    return {name: rt.digest(SCRIPT_DIR / name) for name in IMPLEMENTATION}


def load_job(root, verify_code=True):
    identity = read(root / ".job/identity.json")
    if identity.get("format") != FORMAT:
        raise ValueError("Not a reusable-runner job; keep its existing exact runner, do not adopt it")
    plan = rt.fingerprint(identity)
    if read(root / ".job/plan.json")["plan_id"] != plan:
        raise ValueError("Job identity/plan changed")
    if verify_code and implementation_hashes() != identity["implementation_sha256"]:
        raise ValueError("Installed runner changed; review/version the migration before resume")
    for name, key in (("product_annotation.yaml", "config_sha256"), ("manifest/inputs.json", "inputs_sha256")):
        if rt.digest(root / name) != identity[key]:
            raise ValueError(f"Pinned job artifact changed: {name}")
    items = read(root / "manifest/inputs.json")
    if len(items) != identity["count"]:
        raise ValueError("Input count changed")
    return identity, items, plan


def prepare(args):
    root = args.job_dir.expanduser().absolute()
    if root.exists() and any(root.iterdir()):
        raise ValueError("Prepare requires a new/empty output directory; never adopt existing outputs")
    config = config_data(args.config)
    base_url = rt.https_endpoint(args.base_url)
    if args.credential_source == "dotenv" and args.credential_file:
        # Check ownership/mode and literal assignment without retaining/printing the key.
        credentials.dotenv_key(args.credential_file.expanduser().resolve(), args.credential_profile)
    items = inputs_from(args.images, args.manifest)
    aws = None
    if any(item["kind"] == "s3" for item in items):
        if not all((args.aws_profile, args.aws_account, args.aws_arn)):
            raise ValueError("S3 jobs require explicit AWS profile, account and principal ARN")
        aws = {"profile": args.aws_profile, "account": args.aws_account, "arn": args.aws_arn}
    if args.credential_source == "dotenv" and not args.credential_file:
        raise ValueError("An explicit owner-only credential file is required")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.credential_profile) and args.credential_source == "dotenv":
        raise ValueError("Invalid literal credential variable name")
    if args.crop and (min(args.crop) < 0 or args.crop[0] >= args.crop[2] or args.crop[1] >= args.crop[3]):
        raise ValueError("Invalid crop box")
    root.mkdir(parents=True, exist_ok=True)
    (root / ".job").mkdir()
    with job_lock(root):
        (root / "manifest").mkdir()
        shutil.copy2(args.config, root / "product_annotation.yaml")
        rt.atomic(root / "manifest/inputs.json", items)
        identity = {
            "format": FORMAT, "account": args.account, "model": config["model"],
            "categories": [label["name"] for label in config["labels"]],
            "credential_source": args.credential_source, "credential_profile": args.credential_profile,
            "credential_file": str(args.credential_file.expanduser().resolve()) if args.credential_file else None,
            "base_url": base_url, "aws": aws,
            "count": len(items), "sku_count": len({sku for item in items for sku in item["skus"]}),
            "source_bytes": sum(item["source_bytes"] for item in items),
            "config_sha256": rt.digest(root / "product_annotation.yaml"),
            "inputs_sha256": rt.digest(root / "manifest/inputs.json"),
            "implementation_sha256": implementation_hashes(),
            "workers": args.workers, "max_output_tokens": args.max_output_tokens,
            "crop_box": args.crop, "image_detail": args.image_detail,
            "policy": {"api_total_attempts": args.api_attempts, "s3_total_attempts": args.s3_attempts,
                       "sdk_max_retries": 0, "backoff_seconds": [1, 3, 9, 27],
                       "auth_recoveries_per_read": 2, "auth_max_probes": 10,
                       "auth_pause_seconds": 86400, "heartbeat_seconds": 30,
                       "reserve_bytes": args.reserve_gib * 1024**3},
        }
        rt.atomic(root / ".job/identity.json", identity)
        plan = rt.fingerprint(identity)
        rt.atomic(root / ".job/plan.json", {"plan_id": plan, "created_at": rt.stamp()})
        rt.atomic(root / ".job/state.json", {"status": "prepared", "total": len(items), "completed": 0,
                                            "remaining": len(items), "updated_at": rt.stamp(),
                                            "account": args.account, "model": config["model"]})
    return {"status": "prepared", "plan_id": plan, "count": len(items),
            "source_bytes": identity["source_bytes"], "account": args.account, "model": config["model"]}


def model_key(identity):
    if identity["credential_source"] == "dotenv":
        return credentials.dotenv_key(Path(identity["credential_file"]), identity["credential_profile"])
    # Injection is performed by the installed Agent Secrets CLI, never a shell.
    if not os.environ.get("_SKU_JOB_INJECTED") or not os.environ.get("OPENAI_API_KEY"):
        raise ValueError("Agent Secrets injection is required")
    return os.environ["OPENAI_API_KEY"]


def inject_if_needed(identity):
    if identity["credential_source"] != "agent-secrets" or os.environ.get("_SKU_JOB_INJECTED"):
        return None
    environment = {key: value for key, value in os.environ.items() if not key.startswith("OPENAI_")}
    environment["_SKU_JOB_INJECTED"] = "1"
    secrets = Path.home() / ".codex/skills/agent-secrets/scripts/agent_secrets.py"
    return subprocess.run([
        sys.executable, str(secrets), "run", "--profile", identity["credential_profile"],
        "--env", "OPENAI_API_KEY=private.api_key", "--", sys.executable, "-B", str(Path(__file__)),
        *sys.argv[1:],
    ], env=environment, check=False).returncode


def model_client(identity):
    key = model_key(identity)
    rt.register_secret(key)
    for name in tuple(os.environ):
        if name.startswith("OPENAI_") and not (
            name == "OPENAI_API_KEY" and identity["credential_source"] == "agent-secrets"
        ):
            os.environ.pop(name)
    return OpenAI(api_key=key, base_url=identity["base_url"], max_retries=0, timeout=120)


def preflight(root, account, scope="all"):
    identity, _, _ = load_job(root)
    if identity["account"] != account:
        raise ValueError("Account differs from the recorded job; no substitution permitted")
    result = {"checked_at": rt.stamp(), "account": account, "model": identity["model"],
              "model_calls": 0, "s3_image_reads": 0}
    if scope in ("all", "aws") and identity["aws"]:
        gate = rt.AwsGate(identity, threading.Event(), lambda _: None)
        try:
            result["aws_identity"] = gate.probe()
            result["aws_lifetime"] = rt.lifetime(identity)
        finally:
            gate.close()
    if scope in ("all", "model"):
        with model_client(identity) as client:
            payload = client.with_raw_response.models.list().http_response.json()
        result["model_available"] = identity["model"] in [
            item.get("slug", item.get("id")) for item in payload.get("models", payload.get("data", []))]
        if not result["model_available"]:
            raise ValueError("Exact recorded model not advertised for selected account")
    return dict(result, status="ok")


class Job:
    def __init__(self, root):
        self.root = root
        self.identity, self.items, self.plan = load_job(root)
        self.model = rt.CategoryModel(config_data(root / "product_annotation.yaml"), self.identity)
        self.stop = threading.Event()
        self.auth_status = {"status": "ready"}
        self.aws = rt.AwsGate(self.identity, self.stop, self.publish_auth) if self.identity["aws"] else None
        self.initial_completed = 0
        self.started = time.monotonic()
        self.statistics = Counter()
        self.stats_lock = threading.Lock()

    def cp(self, index):
        return self.root / f".job/records/image-{index:05}.json"

    def path(self, kind, index):
        suffix = ".txt" if kind == "labels" else ".png"
        return self.root / kind / f"{index:05}{suffix}"

    def publish_auth(self, status):
        self.auth_status = dict(status, updated_at=rt.stamp())
        rt.atomic(self.root / ".job/auth-status.json", self.auth_status)

    def validate_record(self, record, item):
        if record.get("plan_id") != self.plan or record.get("item") != item:
            raise ValueError("Checkpoint source/plan differs")
        if record["status"] == "success":
            if set(record.get("outputs", {})) != {"crops", "annotated", "labels"}:
                raise ValueError("Successful checkpoint lacks its complete artifact set")
            if self.model.label(record["response"]) != record["label"]:
                raise ValueError("Checkpoint label differs from response")
            for kind, expected in record["outputs"].items():
                path = self.path(kind, item["index"])
                if rt.digest(path) != expected:
                    raise ValueError("Successful output changed; never silently reclassify")
                if kind != "labels":
                    with Image.open(path) as image:
                        image.verify()
            if self.path("labels", item["index"]).read_text().strip() != record["label"]:
                raise ValueError("Label file differs")
        elif record["status"] not in ("new", "crop_saved", "response_received"):
            raise ValueError(f"Image {item['index']} has failed/uncertain work; explicit recovery review required")
        elif record["status"] in ("crop_saved", "response_received"):
            if rt.digest(self.path("crops", item["index"])) != record["crop_sha256"]:
                raise ValueError("Saved crop changed")

    def audit(self):
        records = {}
        expected = {item["index"]: item for item in self.items}
        for cp in sorted((self.root / ".job/records").glob("image-*.json")):
            record = read(cp)
            index = record["item"]["index"]
            if cp != self.cp(index) or index in records or index not in expected:
                raise ValueError("Unknown/duplicate checkpoint")
            self.validate_record(record, expected[index])
            records[index] = record
        return records

    def heartbeat(self, records, status, failure=None):
        successful = [record for record in records.values() if record["status"] == "success"]
        elapsed = time.monotonic() - self.started
        new = len(successful) - self.initial_completed
        rate = new / elapsed if elapsed else 0
        if status == "running" and self.auth_status["status"] == "paused_auth":
            status = "paused_auth"
        state = {"status": status, "pid": os.getpid(), "account": self.identity["account"],
                 "model": self.identity["model"], "plan_id": self.plan,
                 "total": len(self.items), "completed": len(successful),
                 "remaining": len(self.items) - len(successful),
                 "completed_this_run": new, "updated_at": rt.stamp(),
                 "images_per_minute_this_run": round(rate * 60, 2),
                 "eta_seconds": round((len(self.items) - len(successful)) / rate) if rate and elapsed >= 120 else None,
                 "api_sdk_max_retries": 0, "api_application_max_retries": self.identity["policy"]["api_total_attempts"] - 1,
                 "s3_max_total_attempts_per_object": self.identity["policy"]["s3_total_attempts"],
                 "failure": failure, "aws_auth": self.auth_status,
                 "category_counts": dict(Counter(record["label"] for record in successful))}
        with self.stats_lock:
            state["retry_statistics_this_resume"] = dict(self.statistics)
        rt.atomic(self.root / ".job/state.json", state)
        print(json.dumps({"event": "heartbeat", **state}), flush=True)
        return state

    def bounded(self, kind, item, operation):
        maximum = self.identity["policy"][f"{kind}_total_attempts"]
        predicate = rt.transient_api if kind == "api" else rt.transient_s3
        for attempt in range(1, maximum + 1):
            if self.stop.is_set():
                raise RuntimeError("Stopped before another attempt")
            journal = self.root / f".job/attempts/{kind}/{item['index']:05}/attempt-{attempt}.json"
            if journal.exists():
                raise ValueError("Prior attempt journal exists; explicit recovery review required")
            entry = {"status": "in_flight", "index": item["index"], "kind": kind,
                     "attempt": attempt, "plan_id": self.plan, "started_at": rt.stamp()}
            rt.atomic(journal, entry)
            with self.stats_lock:
                self.statistics[f"{kind}_requests"] += 1
            try:
                result = operation()
            except Exception as error:
                again = predicate(error) and attempt < maximum and not self.stop.is_set()
                rt.atomic(journal, dict(entry, status="failed", error=rt.error_info(error),
                                       retry_scheduled=again, finished_at=rt.stamp()))
                if not again:
                    raise
                with self.stats_lock:
                    self.statistics[f"{kind}_retries_scheduled"] += 1
                if self.stop.wait(self.identity["policy"]["backoff_seconds"][attempt - 1]):
                    raise RuntimeError("Stopped during retry backoff") from error
            else:
                rt.atomic(journal, dict(entry, status="received", finished_at=rt.stamp()))
                return result
        raise AssertionError("Unreachable")

    def finish(self, record):
        index = record["item"]["index"]
        label = self.model.label(record["response"])
        rt.annotated_crop(self.path("crops", index), label, self.path("annotated", index))
        target = self.path("labels", index)
        target.parent.mkdir(parents=True, exist_ok=True)
        pending = target.with_suffix(".txt.pending")
        pending.write_text(label + "\n")
        pending.replace(target)
        record.update(label=label, status="success", finished_at=rt.stamp(),
                      outputs={kind: rt.digest(self.path(kind, index)) for kind in ("crops", "annotated", "labels")})
        rt.atomic(self.cp(index), record)
        return record

    def process(self, client, item):
        cp = self.cp(item["index"])
        record = read(cp) if cp.exists() else {
            "item": item, "plan_id": self.plan, "status": "new", "account": self.identity["account"]}
        if record["status"] == "success":
            # Completed provenance must never be rewritten on validation failure.
            self.validate_record(record, item)
            return record
        try:
            if record["status"] == "response_received":
                return self.finish(record)
            if record["status"] not in ("new", "crop_saved"):
                raise ValueError("Failed/uncertain checkpoint cannot be automatically replayed")
            if self.stop.is_set():
                return None
            if shutil.disk_usage(self.root).free < self.identity["policy"]["reserve_bytes"]:
                raise RuntimeError("Disk reserve reached")
            if record["status"] == "new":
                record.update(status="reading")
                rt.atomic(cp, record)
                if item["kind"] == "s3":
                    raw = self.bounded("s3", item, lambda: self.aws.read(item))
                else:
                    raw = Path(item["path"]).read_bytes()
                    if len(raw) != item["source_bytes"] or hashlib.sha256(raw).hexdigest() != item["source_sha256"]:
                        raise ValueError("Local source image changed")
                crop = rt.crop_image(raw, self.identity["crop_box"])
                rt.save_png(crop, self.path("crops", item["index"]))
                record.update(status="crop_saved", crop_sha256=rt.digest(self.path("crops", item["index"])))
                rt.atomic(cp, record)
            if self.stop.is_set():
                return None
            if self.aws:
                self.aws.gate()
            record.update(status="api_in_flight", request_started_at=rt.stamp())
            rt.atomic(cp, record)
            response = self.bounded("api", item, lambda: self.model.request(
                client, self.path("crops", item["index"])))
            record.update(status="response_received", response=rt.response_data(response))
            rt.atomic(cp, record)
            return self.finish(record)
        except Exception as error:
            self.stop.set()
            record.update(status="failed", error=rt.error_info(error), stopped_at=rt.stamp())
            rt.atomic(cp, record)
            raise

    def run(self):
        with job_lock(self.root):
            records = {}
            try:
                self.heartbeat(records, "validating")
                records = self.audit()
                self.initial_completed = sum(record["status"] == "success" for record in records.values())
                self.started = time.monotonic()
                self.heartbeat(records, "preflighting")
                preflight(self.root, self.identity["account"])
                pending = iter(item for item in self.items if records.get(item["index"], {}).get("status") != "success")
                self.heartbeat(records, "running")
                failure = None
                with model_client(self.identity) as client, ThreadPoolExecutor(max_workers=self.identity["workers"]) as pool:
                    active, exhausted, last = {}, False, time.monotonic()
                    while active or not exhausted:
                        while not self.stop.is_set() and len(active) < self.identity["workers"] and not exhausted:
                            item = next(pending, None)
                            exhausted = item is None
                            if item is not None:
                                active[pool.submit(self.process, client, item)] = item
                        if not active:
                            break
                        done, _ = wait(active, timeout=1, return_when=FIRST_COMPLETED)
                        for future in done:
                            item = active.pop(future)
                            try:
                                result = future.result()
                                if result:
                                    records[item["index"]] = result
                            except Exception as error:
                                failure = {"index": item["index"], **rt.error_info(error)}
                                self.stop.set()
                        if time.monotonic() - last >= self.identity["policy"]["heartbeat_seconds"] or self.stop.is_set():
                            self.heartbeat(records, "stopping" if self.stop.is_set() else "running", failure)
                            last = time.monotonic()
                complete = len(records) == len(self.items) and all(r["status"] == "success" for r in records.values())
                if complete:
                    self.audit()
                    self.export(records)
                state = self.heartbeat(records, "complete" if complete else ("stopped_error" if failure else "stopped"), failure)
                return 0 if state["status"] == "complete" else 1
            except BaseException as error:
                state = self.heartbeat(records, "stopped_error", rt.error_info(error))
                rt.atomic(self.root / ".job/last-error.json", state["failure"])
                raise
            finally:
                if self.aws:
                    self.aws.close()

    def export(self, records):
        pending = self.root / "classification-results.csv.pending"
        with pending.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=("index", "sku", "uri", "label", "crop_file", "annotated_crop_file"))
            writer.writeheader()
            for item in self.items:
                index = item["index"]
                for sku in item["skus"] or [""]:
                    writer.writerow({"index": index, "sku": sku, "uri": item["uri"],
                                     "label": records[index]["label"], "crop_file": f"crops/{index:05}.png",
                                     "annotated_crop_file": f"annotated/{index:05}.png"})
        pending.replace(self.root / "classification-results.csv")


def recover(args):
    """Archive and reset only proven pre-model work, or finish a valid saved response."""
    job = Job(args.job_dir)
    if args.account != job.identity["account"] or args.plan_id != job.plan:
        raise ValueError("Recovery account/plan differs")
    with job_lock(job.root):
        planned = []
        for index in args.index:
            if index < 1 or index > len(job.items):
                raise ValueError("Unknown input index")
            cp = job.cp(index)
            record = read(cp)
            if record.get("plan_id") != job.plan or record["item"] != job.items[index - 1]:
                raise ValueError("Recovery checkpoint identity differs")
            if record["status"] not in ("failed", "reading", "api_in_flight"):
                raise ValueError("Checkpoint is not failed/interrupted work")
            if args.finish_response:
                job.model.label(record["response"])
                if rt.digest(job.path("crops", index)) != record["crop_sha256"]:
                    raise ValueError("Saved crop changed")
                status = "response_received"
            else:
                attempts = job.root / f".job/attempts/api/{index:05}"
                if (record.get("request_started_at") or "response" in record
                        or any(attempts.glob("attempt-*.json"))):
                    raise ValueError("A model request may have occurred; no automatic budget reset or replay")
                status = "crop_saved" if record.get("crop_sha256") else "new"
                if status == "crop_saved" and rt.digest(job.path("crops", index)) != record["crop_sha256"]:
                    raise ValueError("Saved crop changed")
            planned.append((cp, record, status))
        # Validate every preserved success before any recovery mutation.
        for item in job.items:
            cp = job.cp(item["index"])
            if cp.exists() and read(cp)["status"] == "success":
                job.validate_record(read(cp), item)
        directory = job.root / ".job/recoveries" / rt.fingerprint({
            "plan": job.plan, "records": {str(cp): rt.digest(cp) for cp, _, _ in planned}})
        if directory.exists():
            raise ValueError("This recovery was already prepared")
        directory.mkdir(parents=True)
        for cp, record, status in planned:
            shutil.copy2(cp, directory / cp.name)
            if status == "new":
                old_attempts = job.root / f".job/attempts/s3/{record['item']['index']:05}"
                if old_attempts.exists():
                    old_attempts.rename(directory / f"s3-{record['item']['index']:05}")
            record = {key: value for key, value in record.items() if key not in ("error", "stopped_at")}
            record.update(status=status, recovery_archive=str(directory.relative_to(job.root)))
            rt.atomic(cp, record)
        rt.atomic(directory / "authorization.json", {"plan_id": job.plan, "indices": args.index,
                                                     "reason": args.reason, "created_at": rt.stamp(),
                                                     "model_replays_authorized": False})
    return {"status": "recovery_prepared", "indices": args.index, "model_calls": 0}


def add_execution(parser):
    parser.add_argument("--job-dir", type=Path, required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--plan-id", required=True)
    parser.add_argument("--approved-billable-run", action="store_true", required=True)
    parser.add_argument("--approved-s3-reads", action="store_true")


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    sub = cli.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--job-dir", type=Path, required=True)
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--images", type=Path)
    source.add_argument("--manifest", type=Path)
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--account", required=True)
    p.add_argument("--credential-source", choices=("dotenv", "agent-secrets"), required=True)
    p.add_argument("--credential-profile", required=True)
    p.add_argument("--credential-file", type=Path)
    p.add_argument("--base-url", required=True)
    p.add_argument("--aws-profile")
    p.add_argument("--aws-account")
    p.add_argument("--aws-arn")
    p.add_argument("--workers", type=int, choices=range(1, 33), default=4)
    p.add_argument("--max-output-tokens", type=int, required=True)
    p.add_argument("--crop", type=int, nargs=4)
    p.add_argument("--image-detail", choices=("high", "low", "auto"), default="high")
    p.add_argument("--api-attempts", type=int, choices=range(1, 6), default=3)
    p.add_argument("--s3-attempts", type=int, choices=range(1, 6), default=3)
    p.add_argument("--reserve-gib", type=int, default=10)
    p.add_argument("--approved-write", action="store_true", required=True)
    for command in ("inspect", "validate", "status", "diagnose", "monitor-preview"):
        p = sub.add_parser(command)
        p.add_argument("--job-dir", type=Path, required=True)
        if command == "status":
            p.add_argument("--service", action="append", default=[])
    p = sub.add_parser("aws-lifetime")
    p.add_argument("--job-dir", type=Path, required=True)
    p = sub.add_parser("preflight")
    p.add_argument("--job-dir", type=Path, required=True)
    p.add_argument("--account", required=True)
    p.add_argument("--scope", choices=("all", "model", "aws"), default="all")
    for command in ("run", "start"):
        p = sub.add_parser(command)
        add_execution(p)
        if command == "start":
            p.add_argument("--approved-thread-updates", action="store_true")
    p = sub.add_parser("recover")
    p.add_argument("--job-dir", type=Path, required=True)
    p.add_argument("--account", required=True)
    p.add_argument("--plan-id", required=True)
    p.add_argument("--index", type=int, action="append", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--finish-response", action="store_true")
    p.add_argument("--approved-write", action="store_true", required=True)
    p = sub.add_parser("monitor-config")
    p.add_argument("--job-dir", type=Path, required=True)
    p.add_argument("--channel-id", required=True)
    p.add_argument("--thread-ts", required=True)
    p.add_argument("--label", required=True)
    p.add_argument("--interval-seconds", type=int, default=300)
    p.add_argument("--credential-file", type=Path, required=True)
    p.add_argument("--credential-profile", required=True)
    p.add_argument("--gateway-url", required=True)
    p.add_argument("--approved-write", action="store_true", required=True)
    p = sub.add_parser("monitor")
    p.add_argument("--job-dir", type=Path, required=True)
    p.add_argument("--approved-thread-updates", action="store_true", required=True)
    sub.add_parser("test")
    return cli


def main():
    args = parser().parse_args()
    if args.command == "test":
        return subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                               str(SCRIPT_DIR / "test_job_runner.py")], check=False).returncode
    if args.command == "prepare":
        if args.max_output_tokens < 1 or args.reserve_gib < 0:
            raise ValueError("Invalid token cap or disk reserve")
        result = prepare(args)
    else:
        args.job_dir = args.job_dir.expanduser().resolve()
        if args.command == "status":
            services = args.service
            service_file = args.job_dir / ".job/services.json"
            if not services and service_file.exists():
                services = list(read(service_file).values())
            if len(services) > 4:
                raise ValueError("At most four service reads")
            result = checks.job_status(args.job_dir, services)
        elif args.command == "diagnose":
            result = diagnosis(args.job_dir)
        elif args.command == "recover":
            result = recover(args)
        else:
            identity, items, plan = load_job(args.job_dir)
            if args.command == "inspect":
                result = {"plan_id": plan, "identity": identity, "model_calls": 0}
            elif args.command == "aws-lifetime":
                result = {"checked_at": rt.stamp(), "aws_lifetime": rt.lifetime(identity)}
            elif args.command == "validate":
                with job_lock(args.job_dir):
                    records = Job(args.job_dir).audit()
                result = {"status": "valid", "completed": sum(r["status"] == "success" for r in records.values()),
                          "total": len(items), "model_calls": 0}
            elif args.command == "monitor-preview":
                import job_monitor
                config = read(args.job_dir / ".job/monitor.json")
                state = read(args.job_dir / ".job/state.json")
                result = {"message": job_monitor.message(args.job_dir, identity, config, state),
                          "messages_sent": 0}
            elif args.command == "preflight":
                injected = inject_if_needed(identity) if args.scope != "aws" else None
                if injected is not None:
                    return injected
                result = preflight(args.job_dir, args.account, args.scope)
            elif args.command in ("run", "start"):
                if args.account != identity["account"] or args.plan_id != plan:
                    raise ValueError("Explicit account/plan does not match the job")
                if identity["aws"] and not args.approved_s3_reads:
                    raise ValueError("S3 image-read approval is required")
                if args.command == "start":
                    import job_control
                    result = job_control.start(args, identity, plan)
                else:
                    injected = inject_if_needed(identity)
                    if injected is not None:
                        return injected
                    job = Job(args.job_dir)
                    for signum in (signal.SIGINT, signal.SIGTERM):
                        signal.signal(signum, lambda *_: job.stop.set())
                    return job.run()
            else:
                import job_monitor
                if args.command == "monitor-config":
                    result = job_monitor.configure(args)
                else:
                    return job_monitor.watch(args.job_dir)
    print(json.dumps(result, indent=2), flush=True)
    return 0


def diagnosis(root):
    if read(root / ".job/identity.json").get("format") != FORMAT:
        return checks.diagnose(root)
    state = read(root / ".job/state.json")
    failures = []
    for cp in sorted((root / ".job/records").glob("image-*.json")):
        record = read(cp)
        if record.get("status") in ("failed", "api_in_flight", "reading"):
            failures.append({
                "index": record["item"]["index"], "status": record["status"],
                "stage": "model_request_started" if record.get("request_started_at") else "before_model_request",
                "error": record.get("error"),
                "action": "Inspect attempt journals. Never replay uncertain model work; "
                          "use explicit pre-model or saved-response recovery only.",
            })
    return {"checked_at": rt.stamp(), "status": state["status"], "failure": state.get("failure"),
            "failures": failures[-10:], "model_calls": 0}


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"status": "error", "at": rt.stamp(), **rt.error_info(error)}), flush=True)
        raise SystemExit(1)
