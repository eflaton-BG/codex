"""Bounded SKU-run diagnostics. No inference, S3 bodies, login, or service writes."""
from __future__ import annotations

import argparse
import configparser
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

HOME = Path("/home/ezekiel.flaton")
TRUSTED_ENDPOINT = "https://agents-gateway.berkshiregrey.com/ai-gateway/codex/v1"
STATE_KEYS = (
    "status", "pid", "account", "model", "total", "completed", "remaining",
    "failures_this_run", "updated_at", "free_bytes", "images_per_minute_this_run",
    "eta_seconds", "api_sdk_max_retries", "api_application_max_retries",
    "retry_statistics_this_resume", "s3_max_total_attempts_per_object", "pause_reason",
)


def timestamp():
    return datetime.now().astimezone().isoformat()


def sanitize(value):
    text = str(value)
    text = re.sub(r"data:image/[^\s]+", "[image-data-redacted]", text, flags=re.I)
    text = re.sub(r"\bBearer\s+\S+", "Bearer [redacted]", text, flags=re.I)
    text = re.sub(r"(?i)\b(authorization|api[-_ ]?key|token)(\s*[:=]\s*)[^\s,;]+",
                  r"\1\2[redacted]", text)
    text = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?[redacted]", text)
    text = re.sub(r"[A-Za-z0-9_+/=-]{100,}", "[opaque-data-redacted]", text)
    return text[:1600]


def read_json(path):
    with path.open() as stream:
        return json.load(stream)


def aws_lifetime(home=HOME, environ=None, now=None):
    """Read local cache metadata only; credential expiry is not login expiry."""
    environ = os.environ if environ is None else environ
    now = datetime.now(timezone.utc) if now is None else now
    config = configparser.RawConfigParser()
    config.read(environ.get("AWS_CONFIG_FILE", str(home / ".aws/config")))
    profile = environ.get("AWS_PROFILE") or environ.get("AWS_DEFAULT_PROFILE") or "default"
    section = "default" if profile == "default" else f"profile {profile}"
    result = {
        "profile": profile, "provider": "unknown", "access_credentials_expire_at": None,
        "login_session_expires_at": None, "seconds_remaining": None,
        "refresh_material_present": False, "warning": None,
    }
    if "AWS_ACCESS_KEY_ID" in environ:
        result.update(provider="environment",
                      warning="Environment credentials take precedence; expiry is not available from their values.")
        return result
    if not config.has_option(section, "login_session"):
        result["warning"] = "AWS login cache is not configured for this profile; expiry is unknown."
        return result
    result["provider"] = "login"
    session = config.get(section, "login_session")
    directory = Path(os.path.expandvars(os.path.expanduser(
        environ.get("AWS_LOGIN_CACHE_DIRECTORY", str(home / ".aws/login/cache"))
    )))
    cache = directory / (hashlib.sha256(session.encode()).hexdigest() + ".json")
    if not cache.exists():
        result["warning"] = "No cached AWS login session. Run aws login yourself."
        return result
    token = read_json(cache)
    expiry = (token.get("accessToken") or {}).get("expiresAt")
    result.update(
        access_credentials_expire_at=expiry,
        refresh_material_present=bool(token.get("refreshToken")),
        login_session_expires_at=token.get("refreshTokenExpiresAt") or token.get("sessionExpiresAt"),
    )
    if expiry:
        expiry_time = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        if expiry_time.tzinfo is None:
            raise ValueError("AWS cache expiry has no timezone")
        result["seconds_remaining"] = int((expiry_time - now).total_seconds())
        result["access_credentials_expire_local"] = expiry_time.astimezone().isoformat()
        if result["seconds_remaining"] <= 0:
            result["warning"] = "Cached access credentials expired; refresh may require user aws login."
        elif result["seconds_remaining"] <= 300:
            result["warning"] = "Cached access credentials expire within five minutes; verify automatic refresh."
    result["expiry_note"] = (
        "Access credentials can refresh during the run. Their expiry is not the login session's hard expiry; "
        "the cache does not necessarily expose the latter."
    )
    return result


def service_status(name, run=subprocess.run):
    if not re.fullmatch(r"codex-[A-Za-z0-9_.@-]{1,160}\.service", name):
        raise ValueError("Only codex-*.service user units can be inspected")
    response = run(
        ["/usr/bin/systemctl", "--user", "show", name,
         "--property=Id,LoadState,ActiveState,SubState,MainPID,ExecMainStatus,Restart"],
        capture_output=True, text=True, timeout=8, check=False,
    )
    if response.returncode:
        return {"unit": name, "check_error": sanitize(response.stderr.strip()),
                "returncode": response.returncode}
    return dict(line.split("=", 1) for line in response.stdout.splitlines() if "=" in line)


def job_status(root, services=()):
    meta = root / ".job"
    state = read_json(meta / "state.json")
    result = {"checked_at": timestamp(), "job_dir": str(root),
              "state": {key: state[key] for key in STATE_KEYS if key in state}}
    if state.get("updated_at"):
        updated = datetime.fromisoformat(state["updated_at"])
        result["state_age_seconds"] = round((datetime.now().astimezone() - updated).total_seconds())
    for key, path, fields in (
        ("last_error", meta / "last-error.json", ("error_type", "http_status", "at", "completed")),
        ("slack_receipt", meta / "slack/latest-receipt.json",
         ("status", "sent_at", "run_status", "completed", "channel", "thread_ts", "ts")),
    ):
        if path.exists():
            data = read_json(path)
            result[key] = {field: data[field] for field in fields if field in data}
    if services:
        result["services"] = [service_status(name) for name in services]
    return result


def tail_events(path, byte_limit=2 * 1024**2):
    if not path.exists():
        return []
    with path.open("rb") as stream:
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(max(0, size - byte_limit))
        if size > byte_limit:
            stream.readline()
        lines = stream.read(byte_limit).decode(errors="replace").splitlines()
    events = []
    for line in lines:
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def explain_failure(record):
    diagnostic = record.get("error_diagnostic") or {}
    chain = diagnostic.get("chain") or []
    messages = [sanitize(entry.get("message", "")) for entry in chain]
    kind = record.get("error_type") or diagnostic.get("type")
    text = " ".join(messages)
    if "ExpiredToken" in text or kind in ("LoginRefreshRequired", "TokenRetrievalError"):
        reason = "AWS credentials expired or require refresh"
        action = "Run aws login yourself; verify the same AWS identity before safe checkpoint recovery."
    elif kind in ("APIConnectionError", "APITimeoutError"):
        reason = "Model API connection/timeout failure"
        action = "Inspect the per-attempt journal and retry budget; request outcome may be billable."
    elif record.get("http_status") in (401, 403):
        reason = "Authentication or permission failure"
        action = "Verify the selected account/permissions; do not repeatedly retry unchanged credentials."
    else:
        reason = "See saved error details"
        action = "Inspect the checkpoint and attempt journals before replaying it."
    return {
        "index": record.get("index"), "status": record.get("status"), "error_type": kind,
        "http_status": record.get("http_status"), "stopped_at": record.get("stopped_at"),
        "stage": "model_request_started" if record.get("request_started_at") else "before_model_request",
        "reason": reason, "action": action, "messages": messages,
        "cause_types": [entry.get("type") for entry in chain],
        "api_total_attempts": record.get("api_total_attempts"),
        "s3_read_attempts": record.get("s3_read_attempts"),
    }


def diagnose(root, limit=3):
    indices = []
    for event in reversed(tail_events(root / ".job/run.log")):
        index = event.get("index")
        if event.get("event") == "failed" and isinstance(index, int) and index > 0 and index not in indices:
            indices.append(index)
            if len(indices) == limit:
                break
    failures = []
    for index in indices:
        path = root / f".job/records/image-{index:05}.json"
        if path.exists():
            record = read_json(path)
            if record.get("status") == "failed":
                failures.append(explain_failure(record))
    return {"checked_at": timestamp(), "job_dir": str(root), "failures": failures,
            "scope": "Local checkpoints and last 2 MiB of run.log; no provider requests."}


def validate_identity(identity, account):
    if identity.get("account") != account:
        raise ValueError("Selected account does not match the job identity")
    if identity.get("base_url", "").rstrip("/") != TRUSTED_ENDPOINT:
        raise ValueError("Job endpoint is not the trusted BG Codex gateway")
    if identity.get("credential_source") != "dotenv":
        raise ValueError("This preflight supports recorded owner-only dotenv profiles only")
    if Path(identity.get("credential_file", "")).expanduser().resolve() != HOME / ".codex/.env":
        raise ValueError("Only the installed owner-only .codex/.env credential file is allowed")
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,100}", identity.get("credential_profile", "")):
        raise ValueError("Invalid literal credential variable")
    if not identity.get("model"):
        raise ValueError("Job model is missing")


def aws_identity():
    import boto3
    from botocore.config import Config
    with closing(boto3.Session().client("sts", config=Config(
        connect_timeout=5, read_timeout=10, retries={"total_max_attempts": 1},
    ))) as client:
        data = client.get_caller_identity()
    return {"account": data["Account"], "arn": data["Arn"]}


def model_availability(identity):
    # Only import installed skill code, never a Python module from the job directory.
    import annotation_job as guard
    from openai import OpenAI
    key = guard.dotenv_key(HOME / ".codex/.env", identity["credential_profile"])
    with OpenAI(api_key=key, base_url=TRUSTED_ENDPOINT, timeout=20, max_retries=0) as client:
        payload = client.with_raw_response.models.list().http_response.json()
    models = payload.get("models", payload.get("data", []))
    return identity["model"] in [entry.get("slug", entry.get("id")) for entry in models]


def preflight(root, account, scope="all"):
    identity = read_json(root / ".job/identity.json")
    validate_identity(identity, account)
    result = {
        "checked_at": timestamp(), "account": account, "model": identity["model"],
        "model_calls": 0, "s3_image_reads": 0, "aws_lifetime": aws_lifetime(),
    }
    if scope in ("all", "aws"):
        result["aws_identity"] = aws_identity()
        if result["aws_identity"]["account"] != identity["aws_account"]:
            raise ValueError("AWS account differs from the pinned job identity")
        if result["aws_identity"]["arn"] != identity["aws_arn"]:
            raise ValueError("AWS principal differs from the pinned job identity")
        result["aws_lifetime"] = aws_lifetime()
    if scope in ("all", "model"):
        result["model_available"] = model_availability(identity)
        if not result["model_available"]:
            raise ValueError("Exact recorded model is not advertised for the selected account")
    result["status"] = "ok"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status", help="Local state and optional read-only user-service inspection")
    status.add_argument("--job-dir", type=Path, required=True)
    status.add_argument("--service", action="append", default=[])
    diagnosis = sub.add_parser("diagnose", help="Explain failures from local logs/checkpoints")
    diagnosis.add_argument("--job-dir", type=Path, required=True)
    diagnosis.add_argument("--limit", type=int, choices=range(1, 11), default=3)
    sub.add_parser("aws-lifetime", help="Local AWS cache expiry metadata; no network requests")
    check = sub.add_parser("preflight", help="Bounded STS identity and/or model-list requests; no inference")
    check.add_argument("--job-dir", type=Path, required=True)
    check.add_argument("--account", required=True)
    check.add_argument("--scope", choices=("all", "aws", "model"), default="all")
    args = parser.parse_args()
    try:
        if args.command == "status":
            if len(args.service) > 4:
                raise ValueError("At most four service inspections per call")
            result = job_status(args.job_dir.expanduser().resolve(), args.service)
        elif args.command == "diagnose":
            result = diagnose(args.job_dir.expanduser().resolve(), args.limit)
        elif args.command == "aws-lifetime":
            result = {"checked_at": timestamp(), "aws_lifetime": aws_lifetime()}
        else:
            result = preflight(args.job_dir.expanduser().resolve(), args.account, args.scope)
    except Exception as error:
        print(json.dumps({"checked_at": timestamp(), "status": "error",
                          "error_type": type(error).__name__, "message": sanitize(error)}, indent=2))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
