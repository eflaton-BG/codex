#!/bin/bash
# Fixed, skill-owned entry point for the authorized October Stride/Terra job.
set -euo pipefail
SCRIPT_DIR="${BASH_SOURCE[0]%/*}"
PYTHON=/home/ezekiel.flaton/devel/colcon_ws/src/.venv/bin/python
ROOT=/home/ezekiel.flaton/Downloads/pittston-res1-october-2026/runs/terra-revs-v2-11-labels
action="${1:-}"
if [[ $# -gt 0 ]]; then shift; fi
case "$action" in
  status)
    exec "$SCRIPT_DIR/run_checks.sh" status --job-dir "$ROOT" \
      --service codex-pittston-terra-october-2026-resume-3.service \
      --service codex-pittston-terra-october-2026-slack-v3.service "$@" ;;
  diagnose)
    exec "$SCRIPT_DIR/run_checks.sh" diagnose --job-dir "$ROOT" "$@" ;;
  preflight)
    exec "$SCRIPT_DIR/run_checks.sh" preflight --job-dir "$ROOT" --account Stride "$@" ;;
  aws-lifetime)
    exec "$SCRIPT_DIR/run_checks.sh" aws-lifetime "$@" ;;
  audit|prepare|run)
    exec "$PYTHON" -B "$SCRIPT_DIR/resume_auth.py" "$action" "$@" ;;
  test)
    if [[ $# -ne 0 ]]; then exit 2; fi
    exec "$PYTHON" -B -m pytest -q -p no:cacheprovider \
      "$SCRIPT_DIR/test_resume_auth.py" \
      "$ROOT/tools/test_resume_api.py" "$ROOT/tools/test_slack_watch_retry_v2.py" ;;
  gateway-audit|gateway-restart|gateway-run|gateway-watch)
    exec "$PYTHON" -B -u "$SCRIPT_DIR/resume_gateway.py" "${action#gateway-}" "$@" ;;
  gateway-test)
    if [[ $# -ne 0 ]]; then exit 2; fi
    exec "$PYTHON" -B -m pytest -q -p no:cacheprovider \
      "$SCRIPT_DIR/test_resume_gateway.py" "$SCRIPT_DIR/test_resume_auth.py" \
      "$ROOT/tools/test_resume_api.py" "$ROOT/tools/test_slack_watch_retry_v2.py" ;;
  watch-start)
    if [[ $# -ne 1 || "$1" != "--approved-thread-updates" ]]; then exit 2; fi
    exec /usr/bin/systemd-run --user --collect \
      --unit=codex-pittston-terra-october-2026-slack-v3.service \
      --property=Restart=no --property=TimeoutStopSec=60 \
      "--property=StandardOutput=append:$ROOT/.job/slack/watch.log" \
      "--property=StandardError=append:$ROOT/.job/slack/watch.log" \
      "$SCRIPT_DIR/run_october_terra.sh" gateway-watch --approved-thread-updates ;;
  watch)
    if [[ $# -ne 1 || "$1" != "--approved-thread-updates" ]]; then exit 2; fi
    exec "$PYTHON" -B -u "$SCRIPT_DIR/slack_watch_auth_v3.py" ;;
  restart)
    exec "$PYTHON" -B "$SCRIPT_DIR/october_terra_control.py" "$@" ;;
  *)
    printf '%s\n' 'Usage: run_october_terra.sh status|diagnose|preflight|aws-lifetime|audit|test|prepare|run|watch|restart'
    exit 2 ;;
esac
