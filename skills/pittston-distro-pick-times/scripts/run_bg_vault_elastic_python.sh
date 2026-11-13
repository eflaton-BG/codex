#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: bash scripts/run_bg_vault_elastic_python.sh <python args>" >&2
  exit 2
fi

BG_VAULT_ELASTIC_DIR="${BG_VAULT_ELASTIC_DIR:-$HOME/bg-vault-client/bg_vault_elastic}"
BG_ELASTIC_VENV="${BG_ELASTIC_VENV:-}"
BG_ELASTIC_PYTHON="${BG_ELASTIC_PYTHON:-}"
export BG_ELASTIC_CALLER_CWD="${BG_ELASTIC_CALLER_CWD:-$PWD}"

if [[ ! -d "${BG_VAULT_ELASTIC_DIR}" ]]; then
  echo "bg_vault_elastic checkout not found at ${BG_VAULT_ELASTIC_DIR}" >&2
  echo "Set BG_VAULT_ELASTIC_DIR to the checkout directory." >&2
  exit 1
fi

args=("$@")
if [[ "${args[0]}" != -* && -f "${args[0]}" ]]; then
  args[0]="$(cd "$(dirname "${args[0]}")" && pwd)/$(basename "${args[0]}")"
fi

export PYTHONPATH="${BG_VAULT_ELASTIC_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"

if [[ -z "${BG_ELASTIC_PYTHON}" && -n "${BG_ELASTIC_VENV}" ]]; then
  BG_ELASTIC_PYTHON="${BG_ELASTIC_VENV}/bin/python"
fi

if [[ -n "${BG_ELASTIC_PYTHON}" ]]; then
  if [[ ! -x "${BG_ELASTIC_PYTHON}" ]]; then
    echo "Python executable not found at ${BG_ELASTIC_PYTHON}" >&2
    exit 1
  fi
  exec "${BG_ELASTIC_PYTHON}" "${args[@]}"
fi

if command -v python3 >/dev/null 2>&1 &&
  python3 -c 'import bg_vault_elastic, requests' >/dev/null 2>&1; then
  exec python3 "${args[@]}"
fi

if [[ -n "${UV_BIN:-}" ]]; then
  uv_bin="${UV_BIN}"
elif [[ -x "$HOME/.local/bin/uv" ]]; then
  uv_bin="$HOME/.local/bin/uv"
elif command -v uv >/dev/null 2>&1; then
  uv_bin="$(command -v uv)"
else
  echo "No usable Python environment found. Set BG_ELASTIC_PYTHON or install uv." >&2
  exit 1
fi

cd "${BG_VAULT_ELASTIC_DIR}"
exec "${uv_bin}" run python "${args[@]}"
