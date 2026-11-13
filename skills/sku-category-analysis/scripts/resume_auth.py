"""Pinned recovery of pre-model AWS expiry; never replay an uncertain model call."""
from __future__ import annotations

import argparse
from contextlib import closing
import fcntl
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import threading
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

ROOT = Path("/home/ezekiel.flaton/Downloads/pittston-res1-october-2026/runs/terra-revs-v2-11-labels")
spec = importlib.util.spec_from_file_location("api_v2_for_auth", ROOT / "tools/resume_api.py")
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)
r, s = a.r, a.s
sys.path.insert(0, str(r.SKILLS))
import checks

META = ROOT / ".job"
RECOVERY = META / "recovery-auth-v3"
AUTH = RECOVERY / "authorization.json"
FAILED = META / "records/image-21718.json"
OLD_ATTEMPT = META / "recovery-api-bounded-v2/s3-attempts/21718/attempt-1.json"
ORIGINAL_API = a.bounded_api
AUTHORIZATION = None
AWS = None
CLIENT_CONFIG = Config(connect_timeout=5, read_timeout=30, retries={"total_max_attempts": 1})


def auth_error(error):
    if isinstance(error, ClientError):
        return error.response.get("Error", {}).get("Code") in {
            "ExpiredToken", "ExpiredTokenException", "RequestExpired", "InvalidToken",
        }
    return type(error).__name__ in {
        "LoginRefreshRequired", "TokenRetrievalError", "NoCredentialsError",
        "CredentialRetrievalError", "UnauthorizedSSOTokenError",
    }


def safe_pre_model_failure(record):
    return (
        record.get("index") == 21718 and record.get("status") == "failed"
        and record.get("error_type") == "ClientError"
        and any("ExpiredToken" in part.get("message", "")
                for part in record.get("error_diagnostic", {}).get("chain", []))
        and not any(key.startswith(("request_", "requested_", "response_", "crop_", "api_"))
                    for key in record)
    )


def cache_fingerprint():
    """Only local file metadata, never log or expose credential contents."""
    paths = [Path.home() / ".aws/config", Path.home() / ".aws/credentials"]
    paths.extend((Path.home() / ".aws/login/cache").glob("*.json"))
    return tuple(sorted(
        (str(path), path.stat().st_mtime_ns, path.stat().st_size)
        for path in paths if path.exists()
    ))


class AwsRecovery:
    """One native refresh probe, then wait for changed login material, not a retry storm."""

    def __init__(self, root=ROOT):
        self.root = root
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.ready.set()
        self.generation = 0
        self.local = threading.local()
        self.clients = []
        self.clients_lock = threading.Lock()
        self.status = {"status": "ready", "generation": 0}

    def publish(self, **fields):
        self.status = dict(fields, updated_at=r.stamp(), generation=self.generation)
        a.ORIGINAL_WRITE(self.root / ".job/auth-status.json", self.status)
        a.event("aws_auth_status", **self.status)

    def client(self):
        existing = getattr(self.local, "client", None)
        if existing is None or self.local.generation != self.generation:
            if existing is not None:
                existing.close()
            existing = boto3.Session().client("s3", config=CLIENT_CONFIG)
            self.local.client = existing
            self.local.generation = self.generation
            with self.clients_lock:
                self.clients.append(existing)
        return existing

    def probe(self):
        with closing(boto3.Session().client("sts", config=CLIENT_CONFIG)) as client:
            identity = client.get_caller_identity()
        if identity["Account"] != r.AWS_ACCOUNT or identity["Arn"] != r.AWS_ARN:
            raise ValueError("Refreshed AWS identity does not match the pinned job identity")

    def gate(self):
        while not self.ready.wait(1):
            if r.STOP.is_set():
                raise RuntimeError("Stopped while paused for AWS credentials")
        if r.STOP.is_set():
            raise RuntimeError("Stop requested before new work")

    def recover(self, error, index, observed_generation):
        with self.lock:
            if observed_generation != self.generation:
                return
            self.ready.clear()
            started = time.monotonic()
            probes = 0
            fingerprint = None
            try:
                self.publish(
                    status="paused_auth", index=index,
                    reason=f"AWS {type(error).__name__}: {a.sanitized(error)}",
                    action="Trying one fresh AWS session; if that fails, run aws login yourself. "
                           "Resume requires the same verified AWS identity; no model replay.",
                )
                while probes < 10 and time.monotonic() - started < 24 * 3600:
                    if r.STOP.is_set():
                        raise RuntimeError("Stopped while paused for AWS credentials")
                    current = cache_fingerprint()
                    if probes and current == fingerprint:
                        if r.STOP.wait(30):
                            raise RuntimeError("Stopped while paused for AWS credentials")
                        continue
                    probes += 1
                    try:
                        self.probe()
                    except ValueError:
                        raise
                    except Exception as probe_error:
                        fingerprint = cache_fingerprint()
                        self.publish(
                            status="paused_auth", index=index, probes=probes,
                            reason=f"AWS refresh check failed: {type(probe_error).__name__}: "
                                   f"{a.sanitized(probe_error)}",
                            action="Run aws login yourself. Waiting for changed local AWS cache, "
                                   "then verifying the pinned identity. No login is run by this job.",
                        )
                    else:
                        self.generation += 1
                        self.publish(status="ready", index=index, probes=probes,
                                     reason="Fresh AWS session verified against pinned identity")
                        return
                raise RuntimeError("AWS authentication pause exceeded 24 hours or 10 changed-cache checks")
            finally:
                self.ready.set()

    def read(self, _original_client, item):
        for recovery in range(3):
            self.gate()
            generation = self.generation
            try:
                return a.ORIGINAL_READ(self.client(), item)
            except Exception as error:
                if not auth_error(error) or recovery == 2:
                    raise
                self.recover(error, item["index"], generation)
        raise AssertionError("Unreachable")

    def close(self):
        for client in self.clients:
            client.close()


def model_with_auth_gate(*args, **kwargs):
    AWS.gate()
    return ORIGINAL_API(*args, **kwargs)


def write_with_status(path, data):
    if path == META / "state.json":
        auth = AWS.status if AWS is not None else {}
        data["aws_auth"] = dict(auth)
        data["aws_startup_lifetime"] = AUTHORIZATION.get("aws_startup_lifetime")
        if data.get("status") == "running" and auth.get("status") == "paused_auth":
            data.update(status="paused_auth", pause_reason=auth.get("reason"),
                        required_action=auth.get("action"))
    a.write_with_policy(path, data)


def existing_records(identity, validate_files=True):
    """Permit only the one archived, authorized, pre-model recovery checkpoint."""
    records = {}
    by_index = {item["index"]: item for item in identity["inputs"]}
    for cp in sorted((META / "records").glob("image-*.json")):
        record = json.loads(cp.read_text())
        index = record["index"]
        assert index in by_index and index not in records
        for key in ("uri", "skus", "source_bytes", "source_etag"):
            assert record[key] == by_index[index][key]
        if record["status"] == "success":
            r.validate_record(record, by_index[index], identity["categories"], files=validate_files)
        elif record["status"] == "new":
            assert index == 21718 and record["auth_recovery_plan_id"] == AUTHORIZATION["plan_id"]
            assert s.no_model_attempt(record)
        else:
            assert record["status"] in ("crop_saved", "response_received")
        records[index] = record
    return records


def audit():
    identity = json.loads((META / "identity.json").read_text())
    original_plan = json.loads((META / "plan.json").read_text())["plan_id"]
    assert hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest() == original_plan
    assert identity["account"] == "Stride" and identity["model"] == r.MODEL
    r.verify_runtime(identity)
    failed = json.loads(FAILED.read_text())
    assert safe_pre_model_failure(failed), "Only the reviewed pre-model ExpiredToken is authorized"
    successes, crops, failures = {}, {}, []
    by_index = {item["index"]: item for item in identity["inputs"]}
    for cp in sorted((META / "records").glob("image-*.json")):
        record = json.loads(cp.read_text())
        item = by_index[record["index"]]
        for key in ("uri", "skus", "source_bytes", "source_etag"):
            assert record[key] == item[key]
        if record["status"] == "success":
            r.validate_record(record, item, identity["categories"])
            successes[str(cp.relative_to(ROOT))] = r.digest(cp)
        elif record["status"] == "crop_saved":
            assert s.no_model_attempt(record)
            assert r.digest(ROOT / record["crop_file"]) == record["crop_sha256"]
            r.clean_crop(ROOT / record["crop_file"])
            crops[str(cp.relative_to(ROOT))] = r.digest(cp)
        else:
            failures.append(record["index"])
    assert len(successes) == 21717 and failures == [21718]
    prior = json.loads((META / "recovery-api-bounded-v2/authorization.json").read_text())
    assert r.digest(ROOT / "tools/resume_api.py") == prior["identity"]["recovery_runner_sha256"]
    policy = {
        "original_plan_id": original_plan, "account": "Stride", "model": r.MODEL,
        "original_identity_sha256": r.digest(META / "identity.json"),
        "preserved_successes": successes, "preserved_crops": crops,
        "failed_record_sha256": r.digest(FAILED), "failed_s3_attempt_sha256": r.digest(OLD_ATTEMPT),
        "runner_sha256": r.digest(Path(__file__)),
        "watcher_sha256": r.digest(Path(__file__).with_name("slack_watch_auth_v3.py")),
        "api_runner_sha256": r.digest(ROOT / "tools/resume_api.py"),
        "remaining": 38868, "workers": 4, "max_output_tokens": 2048,
        "api_total_attempts_per_image": 3, "api_sdk_retries": 0,
        "s3_transient_attempts": 3, "auth_recoveries_per_s3_attempt": 2,
        "auth_pause_seconds_max": 86400, "auth_probes_per_pause_max": 10,
        "approval": "User requested restart, authentication resilience, and concrete existing-thread Slack updates.",
    }
    return {"plan_id": hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest(),
            "identity": policy, "created_at": r.stamp()}


def prepare():
    with (META / "run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert not AUTH.exists(), "Recovery already prepared; do not reset it"
        authorization = audit()
        policy = authorization["identity"]
        RECOVERY.mkdir(parents=True, exist_ok=True)
        r.copy_verified(FAILED, RECOVERY / "image-21718.original-failed.json",
                        policy["failed_record_sha256"])
        r.copy_verified(OLD_ATTEMPT, RECOVERY / "image-21718.original-s3-attempt-1.json",
                        policy["failed_s3_attempt_sha256"])
        for name, field in (("resume_auth.py", "runner_sha256"),
                            ("slack_watch_auth_v3.py", "watcher_sha256")):
            r.copy_verified(Path(__file__).with_name(name), ROOT / "tools" / name, policy[field])
        a.ORIGINAL_WRITE(AUTH, authorization)
        record = json.loads(FAILED.read_text())
        record = {key: value for key, value in record.items()
                  if not key.startswith("error_") and key not in ("stopped_at", "http_status")}
        record.update(status="new", auth_recovery_plan_id=authorization["plan_id"],
                      original_failure_sha256=policy["failed_record_sha256"],
                      original_failure_archive=str((RECOVERY / "image-21718.original-failed.json").relative_to(ROOT)))
        a.ORIGINAL_WRITE(FAILED, record)
        a.event("auth_recovery_prepared", plan_id=authorization["plan_id"], preserved=21717)


def execute():
    global AUTHORIZATION, AWS
    AUTHORIZATION = json.loads(AUTH.read_text())
    policy = AUTHORIZATION["identity"]
    assert hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest() == AUTHORIZATION["plan_id"]
    assert r.digest(Path(__file__)) == policy["runner_sha256"]
    assert r.digest(META / "identity.json") == policy["original_identity_sha256"]
    assert r.digest(ROOT / "tools/resume_api.py") == policy["api_runner_sha256"]
    for name, expected in (policy["preserved_successes"] | policy["preserved_crops"]).items():
        assert r.digest(ROOT / name) == expected
    assert r.digest(RECOVERY / "image-21718.original-failed.json") == policy["failed_record_sha256"]
    assert r.digest(RECOVERY / "image-21718.original-s3-attempt-1.json") == policy["failed_s3_attempt_sha256"]
    AUTHORIZATION["aws_startup_lifetime"] = checks.aws_lifetime()
    a.ORIGINAL_WRITE(RECOVERY / "startup-aws-lifetime.json", AUTHORIZATION["aws_startup_lifetime"])
    AWS = AwsRecovery()
    AWS.probe()
    AWS.publish(status="ready", reason="Startup identity verified; native AWS refresh permitted")
    a.AUTHORIZATION = s.AUTHORIZATION = AUTHORIZATION
    a.API_RECOVERY = s.RECOVERY = RECOVERY
    s.ORIGINAL_READ = AWS.read
    a.bounded_api = model_with_auth_gate
    r.read_crop = s.bounded_s3_read
    r.process = a.process
    r.existing_records = existing_records
    r.write_json = write_with_status
    r.write_final = a.final_with_policy
    a.event("resume_authorized", account="Stride", model=r.MODEL, preserved=21717,
            recovery_plan_id=AUTHORIZATION["plan_id"], api_retries=2, api_sdk_retries=0)
    try:
        r.run(policy["original_plan_id"])
    finally:
        AWS.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("audit", "prepare", "run"))
    parser.add_argument("--account", required=True, choices=("Stride",))
    parser.add_argument("--approved-restart", action="store_true")
    args = parser.parse_args()
    if args.command == "audit":
        value = audit()
        print(json.dumps({"plan_id": value["plan_id"], "preserved": 21717, "remaining": 38868}))
    else:
        assert args.approved_restart
        (prepare if args.command == "prepare" else execute)()
