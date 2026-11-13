"""Explicit recovery of a rejected gateway request in the pinned legacy job.

This adapter retains the old journals/runtime; new jobs use run_job.sh.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

import checks
import job_monitor
import resume_auth as m
import slack_watch_auth_v3 as watcher

r, a, s = m.r, m.a, m.s
ROOT, META = m.ROOT, m.META
ENTRY = Path(__file__).with_name("run_october_terra.sh")
MAIN = "codex-pittston-terra-october-2026-resume-3.service"
WATCH = "codex-pittston-terra-october-2026-slack-v3.service"
AUTHORIZATION = None


def require(condition, message):
    if not condition:
        raise ValueError(message)


def rejected_request(record, attempt):
    """Only a definite authentication rejection with no response can be reviewed."""
    require(record.get("status") == "failed", "Expected a failed checkpoint")
    require(record.get("error_type") == "AuthenticationError"
            and record.get("http_status") == 401, "Not a definite gateway auth rejection")
    require(record.get("account") == "Stride"
            and record.get("requested_model") == r.MODEL, "Account/model mismatch")
    require(record.get("api_total_attempts") == 1, "Unsupported or exhausted prior budget")
    require(not any(key.startswith("response_") for key in record), "Response needs review")
    require(any(part.get("message") == "invalid Gateway key"
                for part in record.get("error_diagnostic", {}).get("chain", [])),
            "Unexpected gateway rejection")
    require(attempt.get("index") == record["index"] and attempt.get("attempt") == 1
            and attempt.get("status") == "failed", "Attempt journal mismatch")
    require(attempt.get("error", {}).get("type") == "AuthenticationError"
            and attempt.get("error", {}).get("http_status") == 401,
            "Journal does not confirm authentication rejection")
    require(attempt.get("crop_sha256") == record["crop_sha256"]
            and attempt.get("model") == r.MODEL and attempt.get("account") == "Stride",
            "Attempt image/account/model mismatch")
    require(not attempt.get("retry_scheduled"), "Unexpected scheduled retry")
    require(not any(key.startswith("response_") for key in attempt), "Journal has a response")


def recovered_checkpoint(record, authorization, archive):
    """Keep the spent attempt in the budget and retain the original saved crop."""
    result = {
        key: value for key, value in record.items()
        if not key.startswith(("request_", "requested_", "response_", "error_", "api_"))
        and key not in ("http_status", "stopped_at")
    }
    result.update(status="crop_saved", api_prior_attempts=1, api_total_attempts=1,
                  gateway_recovery_plan_id=authorization["plan_id"],
                  original_failure_archive=str(archive.relative_to(ROOT)),
                  original_failure_sha256=authorization["identity"]["failed_sha256"])
    return result


def locations(index):
    directory = META / f"recovery-gateway-{index:05}"
    return directory, directory / "authorization.json", META / f"records/image-{index:05}.json"


def audit(index):
    directory, auth, failed_path = locations(index)
    require(not auth.exists(), "Recovery already exists; do not reset its budget")
    identity = checks.read_json(META / "identity.json")
    original_plan = checks.read_json(META / "plan.json")["plan_id"]
    require(hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            == original_plan, "Original plan mismatch")
    checks.validate_identity(identity, "Stride")
    require(identity["model"] == r.MODEL, "Pinned model mismatch")
    r.verify_runtime(identity)
    previous = checks.read_json(m.AUTH)
    require(r.digest(Path(m.__file__)) == previous["identity"]["runner_sha256"],
            "Pinned AWS recovery implementation changed")
    require(r.digest(ROOT / "tools/resume_api.py") == previous["identity"]["api_runner_sha256"],
            "Pinned API recovery implementation changed")
    record = checks.read_json(failed_path)
    journal_path = ROOT / record["api_attempt_journal"]
    require(journal_path.resolve().is_relative_to((m.RECOVERY / "api-attempts").resolve()),
            "Unexpected attempt journal location")
    rejected_request(record, checks.read_json(journal_path))
    require(record["index"] == index, "Failed image mismatch")
    require(r.digest(ROOT / record["crop_file"]) == record["crop_sha256"], "Crop changed")
    r.clean_crop(ROOT / record["crop_file"])
    inputs = {item["index"]: item for item in identity["inputs"]}
    preserved, failures = {}, []
    successes = 0
    for count, cp in enumerate(sorted((META / "records").glob("image-*.json")), 1):
        current = checks.read_json(cp)
        item = inputs[current["index"]]
        for key in ("index", "uri", "skus", "source_bytes", "source_etag"):
            require(current[key] == item[key], f"Manifest mismatch: {cp.name}/{key}")
        if current["status"] == "success":
            r.validate_record(current, item, identity["categories"])
            successes += 1
        elif current["status"] == "crop_saved":
            require(s.no_model_attempt(current), "Unreviewed prior model attempt")
            require(r.digest(ROOT / current["crop_file"]) == current["crop_sha256"],
                    "Saved crop changed")
            r.clean_crop(ROOT / current["crop_file"])
        else:
            failures.append(current["index"])
        if current["index"] != index:
            preserved[str(cp.relative_to(ROOT))] = r.digest(cp)
        if count % 2000 == 0:
            a.event("gateway_recovery_audit_progress", checked=count, successes=successes)
    require(failures == [index], "Other failed/uncertain checkpoints require review")
    policy = {
        "original_plan_id": original_plan, "account": "Stride", "model": r.MODEL,
        "original_identity_sha256": r.digest(META / "identity.json"),
        "runner_sha256": r.digest(Path(__file__)),
        "auth_runner_sha256": r.digest(Path(m.__file__)),
        "api_runner_sha256": r.digest(ROOT / "tools/resume_api.py"),
        "s3_runner_sha256": r.digest(ROOT / "tools/resume_s3.py"),
        "prior_authorization_sha256": r.digest(m.AUTH),
        "failed_index": index, "failed_sha256": r.digest(failed_path),
        "attempt_path": str(journal_path.relative_to(ROOT)),
        "attempt_sha256": r.digest(journal_path),
        "crop_file": record["crop_file"], "crop_sha256": record["crop_sha256"],
        "preserved_checkpoints": preserved, "preserved_successes_count": successes,
        "remaining": len(inputs) - successes, "api_total_attempts_per_image": 3,
        "api_sdk_retries": 0, "prior_api_attempts": 1,
        "approval": "User requested restart after gateway rejection; retain prior attempt "
                    "and all successes. Failed requests may be billable.",
    }
    return {"plan_id": hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest(),
            "identity": policy, "created_at": r.stamp()}


def prepare(index):
    directory, auth, failed_path = locations(index)
    with (META / "run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        authorization = audit(index)
        policy = authorization["identity"]
        directory.mkdir(parents=True, exist_ok=True)
        archive = directory / "original-failed.json"
        r.copy_verified(failed_path, archive, policy["failed_sha256"])
        r.copy_verified(ROOT / policy["attempt_path"], directory / "original-attempt-1.json",
                        policy["attempt_sha256"])
        a.ORIGINAL_WRITE(auth, authorization)
        a.ORIGINAL_WRITE(failed_path,
                         recovered_checkpoint(checks.read_json(archive), authorization, archive))
        a.event("gateway_recovery_prepared", index=index, plan_id=authorization["plan_id"],
                preserved=policy["preserved_successes_count"], prior_api_attempts=1)
    return authorization


def existing_records(identity, validate_files=True):
    records = {}
    policy = AUTHORIZATION["identity"]
    inputs = {item["index"]: item for item in identity["inputs"]}
    for cp in sorted((META / "records").glob("image-*.json")):
        record = checks.read_json(cp)
        index = record["index"]
        require(index in inputs and index not in records, "Invalid/duplicate checkpoint")
        for key in ("uri", "skus", "source_bytes", "source_etag"):
            require(record[key] == inputs[index][key], "Checkpoint input mismatch")
        if record["status"] == "success":
            r.validate_record(record, inputs[index], identity["categories"], files=validate_files)
        elif record["status"] == "crop_saved":
            if index == policy["failed_index"]:
                require(record.get("gateway_recovery_plan_id") == AUTHORIZATION["plan_id"]
                        and record.get("api_prior_attempts") == 1, "Recovery budget mismatch")
            else:
                require(s.no_model_attempt(record), "Unreviewed prior model attempt")
            require(r.digest(ROOT / record["crop_file"]) == record["crop_sha256"],
                    "Recovery crop changed")
        else:
            raise ValueError(f"Checkpoint {index} requires review; no automatic replay")
        records[index] = record
    return records


def execute(index):
    global AUTHORIZATION
    directory, auth, failed_path = locations(index)
    AUTHORIZATION = checks.read_json(auth)
    policy = AUTHORIZATION["identity"]
    require(hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()
            == AUTHORIZATION["plan_id"], "Recovery plan changed")
    paths = [
        (Path(__file__), "runner_sha256"), (Path(m.__file__), "auth_runner_sha256"),
        (ROOT / "tools/resume_api.py", "api_runner_sha256"),
        (ROOT / "tools/resume_s3.py", "s3_runner_sha256"),
        (META / "identity.json", "original_identity_sha256"),
        (m.AUTH, "prior_authorization_sha256"),
        (directory / "original-failed.json", "failed_sha256"),
        (directory / "original-attempt-1.json", "attempt_sha256"),
        (ROOT / policy["attempt_path"], "attempt_sha256"),
        (ROOT / policy["crop_file"], "crop_sha256"),
    ]
    for path, field in paths:
        require(r.digest(path) == policy[field], f"Pinned artifact changed: {path.name}")
    require(policy["failed_index"] == index, "Recovery index changed")
    for name, digest in policy["preserved_checkpoints"].items():
        require(r.digest(ROOT / name) == digest, "Preserved checkpoint changed")
    # Keep the original attempt directory. Attempt 1 is never deleted or reused.
    m.AUTHORIZATION = a.AUTHORIZATION = s.AUTHORIZATION = AUTHORIZATION
    AUTHORIZATION["aws_startup_lifetime"] = checks.aws_lifetime()
    a.ORIGINAL_WRITE(directory / "startup-aws-lifetime.json",
                     AUTHORIZATION["aws_startup_lifetime"])
    m.AWS = m.AwsRecovery()
    m.AWS.probe()
    m.AWS.publish(status="ready", reason="Startup identity verified after gateway recovery")
    a.API_RECOVERY = s.RECOVERY = m.RECOVERY
    s.ORIGINAL_READ = m.AWS.read
    a.bounded_api = m.model_with_auth_gate
    r.read_crop, r.process = s.bounded_s3_read, a.process
    r.existing_records, r.write_json, r.write_final = existing_records, m.write_with_status, a.final_with_policy
    a.event("gateway_resume_authorized", index=index, account="Stride", model=r.MODEL,
            preserved=policy["preserved_successes_count"], prior_api_attempts=1)
    try:
        r.run(policy["original_plan_id"])
    finally:
        m.AWS.close()


def slack_connection():
    w = watcher.w
    os.environ["BG_AI_GATEWAY_API_KEY"] = w.guard.dotenv_key(
        w.HOME / ".codex/.env", "BG_AI_GATEWAY_API_KEY")
    os.environ["BG_AI_GATEWAY_BASE_URL"] = "https://agents.berkshiregrey.com/v4/ai-gateway"
    os.environ["BG_AI_GATEWAY_TIMEOUT_SECONDS"] = "30"
    w.CONNECTION = job_monitor.connection(w.bga)


def launch(unit, arguments):
    log = META / ("run.log" if unit == MAIN else "slack/watch.log")
    subprocess.run([
        "/usr/bin/systemd-run", "--user", "--collect", f"--unit={unit}",
        "--property=Restart=no", "--property=TimeoutStopSec=180",
        f"--property=StandardOutput=append:{log}", f"--property=StandardError=append:{log}",
        str(ENTRY), *arguments,
    ], check=True, timeout=15)


def restart(index):
    for unit in (MAIN, WATCH, "codex-pittston-terra-october-2026-resume-2.service"):
        require(checks.service_status(unit).get("ActiveState") in ("inactive", "failed"),
                f"Active/unconfirmed service: {unit}")
    identity = checks.read_json(META / "identity.json")
    checks.validate_identity(identity, "Stride")
    require(checks.model_availability(identity), "Selected model unavailable")
    aws = checks.aws_identity()
    require(aws["account"] == r.AWS_ACCOUNT and aws["arn"] == r.AWS_ARN,
            "AWS identity mismatch")
    slack_connection()
    prepare(index)
    launch(MAIN, ["gateway-run", "--index", str(index), "--account", "Stride", "--approved-restart"])
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        service = checks.service_status(MAIN)
        require(service.get("ActiveState") in ("active", "activating"),
                f"Worker exited before heartbeat: {service}")
        state = checks.read_json(META / "state.json")
        if str(state.get("pid")) == service.get("MainPID") and state["status"] == "running":
            launch(WATCH, ["gateway-watch", "--approved-thread-updates"])
            print(json.dumps({"status": "launched", "completed": state["completed"],
                              "main_service": MAIN, "slack_service": WATCH}), flush=True)
            return
        time.sleep(1)
    raise RuntimeError("No fresh heartbeat; inspect the service before any replay")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("audit", "restart", "run", "watch"))
    parser.add_argument("--index", type=int)
    parser.add_argument("--account", choices=("Stride",))
    parser.add_argument("--approved-restart", action="store_true")
    parser.add_argument("--approved-thread-updates", action="store_true")
    args = parser.parse_args()
    if args.command == "watch":
        require(args.approved_thread_updates, "Explicit thread approval required")
        slack_connection()
        watcher.main()
        return
    require(args.index and args.account == "Stride", "Explicit failed index and account required")
    if args.command == "audit":
        value = audit(args.index)
        print(json.dumps({"plan_id": value["plan_id"],
                          "preserved": value["identity"]["preserved_successes_count"]}))
    else:
        require(args.approved_restart, "Explicit restart approval required")
        if args.command == "restart":
            require(args.approved_thread_updates, "Explicit thread approval required")
            restart(args.index)
        else:
            execute(args.index)


if __name__ == "__main__":
    main()
