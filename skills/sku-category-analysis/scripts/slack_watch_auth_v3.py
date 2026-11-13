"""Persistent five-minute thread updates, including actionable AWS pauses."""
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path("/home/ezekiel.flaton/Downloads/pittston-res1-october-2026/runs/terra-revs-v2-11-labels")
spec = importlib.util.spec_from_file_location("watch_v2_for_auth", ROOT / "tools/slack_watch_retry_v2.py")
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
sys.path.insert(0, str(w.HOME / ".codex/skills/sku-category-analysis/scripts"))
import checks

BASE_REQUEST = w.bga.request
SERVICE = "codex-pittston-terra-october-2026-resume-3.service"


def view(state):
    result = dict(state)
    service = checks.service_status(SERVICE)
    if state["status"] not in ("complete", "stopped", "stopped_error"):
        if service.get("ActiveState") in ("failed", "inactive"):
            result.update(status="stopped_error", service_failure=service)
    auth = ROOT / ".job/auth-status.json"
    if auth.exists() and result["status"] not in ("complete", "stopped", "stopped_error"):
        current = json.loads(auth.read_text())
        if current.get("status") == "paused_auth":
            result.update(status="paused_auth", pause_reason=current.get("reason"),
                          required_action=current.get("action"))
    return result


def details(state):
    lines = []
    if state["status"] == "paused_auth":
        lines += [f"PAUSED before new model calls: {state.get('pause_reason')}",
                  f"Action: {state.get('required_action')}"]
    elif state["status"] in ("stopped", "stopped_error"):
        if state.get("service_failure"):
            service = state["service_failure"]
            lines.append(f"Service {service.get('ActiveState')}/{service.get('SubState')}; "
                         f"exit status {service.get('ExecMainStatus')}.")
        failures = checks.diagnose(ROOT)["failures"]
        if failures:
            for failure in failures:
                lines.append(
                    f"Image {failure['index']} | {failure['stage']} | {failure['reason']}: "
                    + "; ".join(failure["messages"])[:600]
                )
                lines.append(f"Action: {failure['action']}")
        else:
            lines.append("No failed image checkpoint explains this stop. "
                         "Inspect the service exit/log; no automatic model replay.")
    lifetime = checks.aws_lifetime()
    expiry = lifetime.get("access_credentials_expire_local") or lifetime.get("access_credentials_expire_at")
    lines.append(
        f"AWS access-credential expiry: {expiry or 'unknown'}; "
        f"refresh material {'present' if lifetime.get('refresh_material_present') else 'not confirmed'}. "
        "This is not the hard login-session expiry."
    )
    lines.append("AWS hard login-session expiry: "
                 + (lifetime.get("login_session_expires_at") or "not exposed by the cache") + ".")
    if lifetime.get("warning"):
        lines.append(lifetime["warning"])
    return "\n".join(lines)


def post(state):
    """Reuse the proven sender/receipt checks, augmenting only the exact Slack payload."""
    state = view(state)
    extra = details(state)

    def augmented_request(path, method="GET", body=None):
        if method == "POST" and path.endswith("/call") and body.get("path") == "/chat.postMessage":
            body = dict(body)
            payload = json.loads(body["bodyText"])
            payload["text"] += "\n" + extra
            body["bodyText"] = json.dumps(payload)
            w.guard.write_json(w.META / "latest-payload.json", payload)
        return BASE_REQUEST(path, method, body)

    w.bga.request = augmented_request
    try:
        w.post(state)
    finally:
        w.bga.request = BASE_REQUEST
    return state["status"]


def main():
    os.environ["BG_AI_GATEWAY_API_KEY"] = w.guard.dotenv_key(w.HOME / ".codex/.env", "BG_AI_GATEWAY_API_KEY")
    os.environ["BG_AI_GATEWAY_BASE_URL"] = "https://agents.berkshiregrey.com/v4/ai-gateway"
    os.environ["BG_AI_GATEWAY_TIMEOUT_SECONDS"] = "30"
    next_send, previous = 0, None
    while True:
        try:
            state = view(json.loads(w.STATE.read_text()))
            terminal = state["status"] in ("complete", "stopped", "stopped_error")
            if time.monotonic() >= next_send or state["status"] != previous:
                post(state)
                previous = state["status"]
                next_send = time.monotonic() + 300
            if terminal:
                return
        except Exception as error:
            print(json.dumps({"event": "watch_error", "type": type(error).__name__,
                              "at": datetime.now().astimezone().isoformat()}), flush=True)
        time.sleep(5)


if __name__ == "__main__":
    main()
