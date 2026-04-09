#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: bash scripts/run_bg_vault_elastic_python.sh <python args>" >&2
  exit 2
fi

BG_VAULT_ELASTIC_DIR="${BG_VAULT_ELASTIC_DIR:-$HOME/bg-vault-client/bg_vault_elastic}"
BG_ELASTIC_VENV="${BG_ELASTIC_VENV:-/home/ezekiel.flaton@berkshiregrey.com/devel/colcon_ws/src/.venv}"
BG_ELASTIC_PYTHON="${BG_ELASTIC_PYTHON:-${BG_ELASTIC_VENV}/bin/python}"

if [[ ! -d "${BG_VAULT_ELASTIC_DIR}" ]]; then
  echo "bg_vault_elastic checkout not found at ${BG_VAULT_ELASTIC_DIR}" >&2
  echo "Set BG_VAULT_ELASTIC_DIR to your bg_vault_elastic checkout directory." >&2
  exit 1
fi

if [[ ! -x "${BG_ELASTIC_PYTHON}" ]]; then
  echo "Python executable not found at ${BG_ELASTIC_PYTHON}" >&2
  echo "Set BG_ELASTIC_PYTHON or BG_ELASTIC_VENV to the venv you want this skill to use." >&2
  exit 1
fi

export PYTHONPATH="${BG_VAULT_ELASTIC_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"
cd "${BG_VAULT_ELASTIC_DIR}"
exec "${BG_ELASTIC_PYTHON}" "$@"
