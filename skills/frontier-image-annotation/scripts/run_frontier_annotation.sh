#!/usr/bin/env bash
set -euo pipefail

readonly PYTHON="/home/ezekiel.flaton/devel/colcon_ws/src/.venv/bin/python"
readonly RUNNER="/home/ezekiel.flaton/.codex/skills/frontier-image-annotation/scripts/frontier_annotation.py"
readonly AGENT_SECRETS="/home/ezekiel.flaton/.codex/skills/agent-secrets/scripts/agent_secrets.py"
readonly PROFILE="${FRONTIER_OPENAI_PROFILE:-openai/transcription}"
readonly BASE_URL="${OPENAI_BASE_URL:-https://agents-gateway.berkshiregrey.com/ai-gateway/codex/v1}"

if [[ ! -x "${PYTHON}" ]]; then
  printf 'Workspace virtualenv Python is unavailable: %s\n' "${PYTHON}" >&2
  exit 1
fi

if [[ ! -f "${RUNNER}" ]]; then
  printf 'Frontier annotation runner is unavailable: %s\n' "${RUNNER}" >&2
  exit 1
fi

case "${1:-}" in
  inspect|annotate)
    if [[ ! -f "${AGENT_SECRETS}" ]]; then
      printf 'Agent Secrets runner is unavailable: %s\n' "${AGENT_SECRETS}" >&2
      exit 1
    fi
    exec /usr/bin/env OPENAI_BASE_URL="${BASE_URL}" \
      "${PYTHON}" "${AGENT_SECRETS}" run \
      --profile "${PROFILE}" \
      --env OPENAI_API_KEY=private.api_key \
      -- "${PYTHON}" "${RUNNER}" "$@"
    ;;
  status|evaluate)
    exec "${PYTHON}" "${RUNNER}" "$@"
    ;;
  *)
    printf 'Usage: %s {inspect|annotate|status|evaluate} [arguments...]\n' "$0" >&2
    exit 2
    ;;
esac
