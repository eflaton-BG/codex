#!/usr/bin/env bash
set -euo pipefail

readonly PYTHON="/home/ezekiel.flaton/devel/colcon_ws/src/.venv/bin/python"
readonly RUNNER="/home/ezekiel.flaton/.codex/skills/frontier-image-annotation/scripts/frontier_annotation.py"
readonly JOB_RUNNER="/home/ezekiel.flaton/.codex/skills/frontier-image-annotation/scripts/annotation_job.py"

if [[ ! -x "${PYTHON}" ]]; then
  printf 'Workspace virtualenv Python is unavailable: %s\n' "${PYTHON}" >&2
  exit 1
fi

if [[ ! -f "${RUNNER}" ]]; then
  printf 'Frontier annotation runner is unavailable: %s\n' "${RUNNER}" >&2
  exit 1
fi

case "${1:-}" in
  annotate)
    shift
    exec "${PYTHON}" "${JOB_RUNNER}" "$@"
    ;;
  inspect|status|evaluate|validate)
    exec "${PYTHON}" "${RUNNER}" "$@"
    ;;
  *)
    printf 'Usage: %s {inspect|annotate|status|evaluate|validate} [arguments...]\n' "$0" >&2
    exit 2
    ;;
esac
