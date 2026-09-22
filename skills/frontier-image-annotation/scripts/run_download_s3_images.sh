#!/usr/bin/env bash
set -euo pipefail

readonly PYTHON="/home/ezekiel.flaton/devel/colcon_ws/src/.venv/bin/python"
readonly SCRIPT_DIR="$(
  /usr/bin/dirname -- "${BASH_SOURCE[0]}"
)"
readonly DOWNLOADER="${SCRIPT_DIR}/download_s3_images.py"

if [[ ! -x "${PYTHON}" ]]; then
  printf 'Workspace virtualenv Python is unavailable: %s\n' "${PYTHON}" >&2
  exit 1
fi

if [[ ! -f "${DOWNLOADER}" ]]; then
  printf 'Bundled downloader is unavailable: %s\n' "${DOWNLOADER}" >&2
  exit 1
fi

exec "${PYTHON}" "${DOWNLOADER}" "$@"
