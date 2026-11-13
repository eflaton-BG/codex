"""Fixed service control for the approved October run; no arbitrary commands."""
import argparse
import json
from pathlib import Path
import subprocess
import time

import checks

ROOT = Path("/home/ezekiel.flaton/Downloads/pittston-res1-october-2026/runs/terra-revs-v2-11-labels")
ENTRY = Path(__file__).with_name("run_october_terra.sh")
MAIN = "codex-pittston-terra-october-2026-resume-3.service"
WATCH = "codex-pittston-terra-october-2026-slack-v3.service"


def launch(unit, log, args):
    subprocess.run([
        "/usr/bin/systemd-run", "--user", "--collect", f"--unit={unit}",
        "--property=Restart=no", "--property=TimeoutStopSec=180",
        f"--property=StandardOutput=append:{log}",
        f"--property=StandardError=append:{log}", str(ENTRY), *args,
    ], check=True, timeout=15)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", required=True, choices=("Stride",))
    parser.add_argument("--approved-restart", required=True, action="store_true")
    parser.add_argument("--approved-thread-updates", required=True, action="store_true")
    parser.parse_args()
    for unit in (MAIN, "codex-pittston-terra-october-2026-resume-2.service"):
        service = checks.service_status(unit)
        if service.get("ActiveState") not in ("inactive", "failed"):
            raise RuntimeError(f"Cannot launch alongside an active/unconfirmed job: {unit}: {service}")
    auth = ROOT / ".job/recovery-auth-v3/authorization.json"
    if not auth.exists():
        subprocess.run([str(ENTRY), "prepare", "--account", "Stride", "--approved-restart"],
                       check=True)
    # Existing recovery is intentionally not reset or automatically replayed.
    launch(MAIN, ROOT / ".job/run.log", ["run", "--account", "Stride", "--approved-restart"])
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        service = checks.service_status(MAIN)
        if service.get("ActiveState") in ("failed", "inactive"):
            raise RuntimeError(f"Run exited before a fresh heartbeat: {service}")
        state = json.loads((ROOT / ".job/state.json").read_text())
        if (str(state.get("pid")) == service.get("MainPID")
                and state.get("status") in ("running", "paused_auth")):
            launch(WATCH, ROOT / ".job/slack/watch.log", ["watch", "--approved-thread-updates"])
            print(json.dumps({"status": "launched", "completed": state["completed"],
                              "main_service": MAIN, "slack_service": WATCH}), flush=True)
            return
        time.sleep(1)
    raise RuntimeError("No fresh heartbeat after 180 seconds; inspect the service before any replay")


if __name__ == "__main__":
    main()
