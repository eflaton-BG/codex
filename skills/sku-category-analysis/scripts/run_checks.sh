#!/bin/bash
# Read-only operations only. Keep this entry point separate from job execution.
set -euo pipefail
readonly PYTHON="/home/ezekiel.flaton/devel/colcon_ws/src/.venv/bin/python"
readonly SCRIPT_DIR="${BASH_SOURCE[0]%/*}"
export PYTHONDONTWRITEBYTECODE=1
exec "${PYTHON}" "${SCRIPT_DIR}/checks.py" "$@"
