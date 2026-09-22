#!/usr/bin/env bash
set -euo pipefail

readonly PYTHON="/home/ezekiel.flaton/devel/colcon_ws/src/.venv/bin/python"
readonly AGENT_SECRETS="/home/ezekiel.flaton/.codex/skills/agent-secrets/scripts/agent_secrets.py"
readonly BG_VAULT_ELASTIC_DIR="/home/ezekiel.flaton/bg-vault-client/bg_vault_elastic"
readonly SCRIPT_DIR="$(
  /usr/bin/dirname -- "${BASH_SOURCE[0]}"
)"
readonly EXPORTER="${SCRIPT_DIR}/export_res1_sku_images.py"

if [[ ! -x "${PYTHON}" ]]; then
  printf 'Workspace virtualenv Python is unavailable: %s\n' "${PYTHON}" >&2
  exit 1
fi
if [[ ! -f "${AGENT_SECRETS}" ]]; then
  printf 'agent-secrets is unavailable: %s\n' "${AGENT_SECRETS}" >&2
  exit 1
fi
if [[ ! -d "${BG_VAULT_ELASTIC_DIR}" ]]; then
  printf 'bg_vault_elastic is unavailable: %s\n' "${BG_VAULT_ELASTIC_DIR}" >&2
  exit 1
fi
if [[ ! -f "${EXPORTER}" ]]; then
  printf 'RES1 exporter is unavailable: %s\n' "${EXPORTER}" >&2
  exit 1
fi

export PYTHONPATH="${BG_VAULT_ELASTIC_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"
exec "${PYTHON}" "${AGENT_SECRETS}" run \
  --profile mongodb/pittston-pickinspector \
  --env MONGO_HOST=public.host \
  --env MONGO_USERNAME=public.username \
  --env MONGO_PASSWORD=private.password \
  --env MONGO_DATABASE=public.database \
  -- "${PYTHON}" "${EXPORTER}" "$@"
