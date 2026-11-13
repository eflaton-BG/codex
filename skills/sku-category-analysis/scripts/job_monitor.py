"""Generic existing-thread Slack monitoring, separate from inference credentials."""
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import re
import time
import uuid

import annotation_job as credentials
import checks
import job_runtime as rt


def configure(args):
    if not re.fullmatch(r"[CGD][A-Z0-9]+", args.channel_id):
        raise ValueError("Use an explicit Slack channel ID")
    if not re.fullmatch(r"\d+\.\d+", args.thread_ts):
        raise ValueError("Use an explicit existing Slack thread timestamp")
    if not 30 <= args.interval_seconds <= 3600:
        raise ValueError("Monitor cadence must be 30..3600 seconds")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.credential_profile):
        raise ValueError("Invalid literal BGA credential variable")
    destination = args.job_dir / ".job/monitor.json"
    if destination.exists():
        raise ValueError("Monitor config already exists; do not change a running destination")
    config = {
        "channel": args.channel_id, "thread_ts": args.thread_ts, "label": args.label,
        "interval_seconds": args.interval_seconds,
        "credential_file": str(args.credential_file.expanduser().resolve()),
        "credential_profile": args.credential_profile,
        "gateway_url": rt.https_endpoint(args.gateway_url),
    }
    rt.atomic(destination, config)
    return {"status": "monitor_configured", "channel": args.channel_id, "thread_ts": args.thread_ts,
            "messages_sent": 0}


def snapshot(root):
    state = json.loads((root / ".job/state.json").read_text())
    terminal = {"complete", "stopped", "stopped_error"}
    services_file = root / ".job/services.json"
    if services_file.exists():
        service = checks.service_status(json.loads(services_file.read_text())["run"])
        if service.get("ActiveState") in ("failed", "inactive") and state["status"] not in terminal:
            state.update(status="stopped_error", failure={
                "error_type": "WorkerServiceExited", "service": service,
                "action": "Inspect the saved worker log and checkpoints before recovery; no automatic replay."})
    auth_file = root / ".job/auth-status.json"
    if auth_file.exists() and state["status"] not in terminal:
        auth = json.loads(auth_file.read_text())
        if auth.get("status") == "paused_auth":
            state.update(status="paused_auth", aws_auth=auth)
    return state


def message(root, identity, config, state):
    now = datetime.now().astimezone()
    total, completed = state["total"], state["completed"]
    lines = [
        f"ZekesCodexFriend | {now.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"{config['label']} | {identity['account']} | {identity['model']} | {state['status']}",
        f"{completed:,}/{total:,} complete; {total - completed:,} remaining.",
        f"API: up to {identity['policy']['api_total_attempts']} total attempts/image; SDK retries 0. "
        f"S3: up to {identity['policy']['s3_total_attempts']} transient attempts/object.",
    ]
    if state.get("eta_seconds") is not None:
        lines.append(f"Measured rate {state['images_per_minute_this_run']:.1f}/min; "
                     f"ETA ~{state['eta_seconds'] / 3600:.1f} hours (estimate).")
    age = (now - datetime.fromisoformat(state["updated_at"])).total_seconds()
    if age > 180 and state["status"] not in {"complete", "stopped", "stopped_error"}:
        lines.append(f"WARNING: state heartbeat {int(age)}s old; progress is not confirmed.")
    auth = state.get("aws_auth", {})
    if state["status"] == "paused_auth":
        lines.append(f"Paused before new model calls: {checks.sanitize(auth.get('reason'))}")
        lines.append(f"Action: {auth.get('action', 'Verify the saved AWS diagnostic and refresh login yourself.')}")
    if state["status"] in {"stopped", "stopped_error"}:
        from job_runner import diagnosis
        info = diagnosis(root)
        lines.append("Stop reason: " + checks.sanitize(state.get("failure") or info.get("failure")
                                                       or "No failure captured; inspect worker exit/log."))
        for failed in info["failures"][-3:]:
            lines.append(f"Image {failed['index']} | {failed['stage']}: "
                         + checks.sanitize(failed["error"]))
            lines.append("Action: " + failed["action"])
        lines.append("Saved outputs retained. No automatic model replay.")
    lifetime = rt.lifetime(identity)
    if lifetime:
        lines.append(
            "AWS access-credential expiry: "
            + (lifetime.get("access_credentials_expire_local") or "unknown")
            + "; refresh material " + ("present" if lifetime.get("refresh_material_present") else "not confirmed")
            + ". This is not the hard login-session expiry."
        )
        lines.append("AWS hard login-session expiry: "
                     + (lifetime.get("login_session_expires_at") or "unknown/not exposed") + ".")
        if lifetime.get("warning"):
            lines.append(lifetime["warning"])
    return "\n".join(lines)


def connection(bga):
    """Rediscover the connected UUID and verify chat.postMessage permission."""
    with bga.request("/bga/v1/connections") as response:
        listing = json.load(response)
    entries = listing if isinstance(listing, list) else listing["connections"]
    selected = [entry for entry in entries if entry.get("connectionRef") == "slack:default"
                and entry.get("status") == "connected" and entry.get("callable")]
    if len(selected) != 1:
        raise ValueError("Expected one connected callable slack:default entry")
    selected_id = str(uuid.UUID(selected[0]["connectionId"]))
    with bga.request(f"/bga/v1/connections/{selected_id}/permissions") as response:
        permissions = json.load(response)
    allowed = any(
        product.get("enabled") and category.get("enabled") and endpoint.get("enabled")
        and endpoint.get("method") == "POST" and endpoint.get("path") == "/chat.postMessage"
        for product in permissions.get("products", [])
        for category in product.get("categories", [])
        for endpoint in category.get("endpoints", [])
    )
    if not allowed:
        raise ValueError("Connected Slack account lacks enabled chat.postMessage permission")
    return selected_id


def post(root, bga, connection_id, config, text, state):
    payload = {"channel": config["channel"], "thread_ts": config["thread_ts"], "text": text,
               "reply_broadcast": False, "unfurl_links": False, "unfurl_media": False,
               "client_msg_id": str(uuid.uuid4())}
    journal = root / ".job/slack" / f"send-{payload['client_msg_id']}.json"
    rt.atomic(journal, {"status": "sending", "payload": payload, "at": rt.stamp()})
    try:
        with bga.request(f"/bga/v1/connections/{connection_id}/call", "POST", {
            "method": "POST", "path": "/chat.postMessage",
            "headers": {"Content-Type": "application/json"}, "bodyText": json.dumps(payload),
        }) as response:
            envelope = json.load(response)
        result = json.loads(envelope["bodyText"])
        if (envelope["status"] != 200 or not result.get("ok") or result.get("channel") != config["channel"]
                or result.get("message", {}).get("thread_ts") != config["thread_ts"]):
            raise ValueError("Slack receipt was not verified")
        receipt = {"status": "sent", "channel": config["channel"], "thread_ts": config["thread_ts"],
                   "ts": result["ts"], "sent_at": rt.stamp(), "completed": state["completed"],
                   "run_status": state["status"]}
        rt.atomic(journal, receipt)
        rt.atomic(root / ".job/slack/latest-receipt.json", receipt)
        return True
    except Exception as error:
        rt.atomic(journal, {"status": "unconfirmed", "error": rt.error_info(error),
                           "at": rt.stamp(), "no_immediate_resend": True})
        return False


def watch(root):
    from job_runner import load_job
    identity, _, _ = load_job(root)
    config = json.loads((root / ".job/monitor.json").read_text())
    key = credentials.dotenv_key(Path(config["credential_file"]), config["credential_profile"])
    rt.register_secret(key)
    os.environ["BG_AI_GATEWAY_API_KEY"] = key
    os.environ["BG_AI_GATEWAY_BASE_URL"] = config["gateway_url"]
    os.environ["BG_AI_GATEWAY_TIMEOUT_SECONDS"] = "30"
    path = Path.home() / ".codex/skills/bga-connections/bga-connections.py"
    spec = importlib.util.spec_from_file_location("category_job_bga", path)
    bga = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bga)
    connection_id = connection(bga)
    next_send, previous = 0, None
    while True:
        try:
            state = snapshot(root)
            terminal = state["status"] in {"complete", "stopped", "stopped_error"}
            if time.monotonic() >= next_send or state["status"] != previous:
                post(root, bga, connection_id, config, message(root, identity, config, state), state)
                next_send = time.monotonic() + config["interval_seconds"]
                previous = state["status"]
            if terminal:
                return 0
        except Exception as error:
            rt.atomic(root / ".job/monitor-error.json", {"at": rt.stamp(), **rt.error_info(error)})
        time.sleep(5)
