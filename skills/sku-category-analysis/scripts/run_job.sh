#!/bin/bash
# All operational code stays in this skill; no shell-sourced credentials or /tmp code.
set -euo pipefail
SCRIPT_DIR="${BASH_SOURCE[0]%/*}"
exec "${HOME}/devel/colcon_ws/src/.venv/bin/python" -B -u "$SCRIPT_DIR/job_runner.py" "$@"
