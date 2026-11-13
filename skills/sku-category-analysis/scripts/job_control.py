"""Deterministic per-job service control; no user-supplied shell or executable."""
import fcntl
from pathlib import Path
import subprocess

import checks
import job_runtime as rt


def units(root, plan):
    stem = "codex-sku-" + rt.fingerprint({"root": str(root.resolve()), "plan": plan})[:16]
    return {"run": stem + "-run.service", "monitor": stem + "-monitor.service"}


def launch(unit, log, arguments):
    subprocess.run([
        "/usr/bin/systemd-run", "--user", "--collect", f"--unit={unit}",
        "--property=Restart=no", "--property=TimeoutStopSec=180",
        f"--property=StandardOutput=append:{log}",
        f"--property=StandardError=append:{log}",
        str(Path(__file__).with_name("run_job.sh")), *arguments,
    ], check=True, timeout=15)


def start(args, identity, plan):
    services = units(args.job_dir, plan)
    for unit in services.values():
        state = checks.service_status(unit)
        if state.get("ActiveState") not in ("inactive", "failed"):
            raise ValueError(f"Job service already active or status unconfirmed: {unit}")
    # Detect a foreground run too. The runner separately holds the same lock
    # through validation, preflight, model work, and finalization.
    with (args.job_dir / ".job/run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    run_args = ["run", "--job-dir", str(args.job_dir), "--account", args.account,
                "--plan-id", plan, "--approved-billable-run"]
    if identity["aws"]:
        run_args.append("--approved-s3-reads")
    if args.approved_thread_updates and not (args.job_dir / ".job/monitor.json").exists():
        raise ValueError("Configure the explicit Slack destination/credentials before approving updates")
    rt.atomic(args.job_dir / ".job/services.json", services)
    launch(services["run"], args.job_dir / ".job/run.log", run_args)
    monitor_started = False
    if args.approved_thread_updates:
        launch(services["monitor"], args.job_dir / ".job/monitor.log",
               ["monitor", "--job-dir", str(args.job_dir), "--approved-thread-updates"])
        monitor_started = True
    return {"status": "launched", "services": services, "monitor_started": monitor_started,
            "note": "Launched is not verified progress; inspect fresh state and successful checkpoints."}
