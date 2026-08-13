#!/usr/bin/env bash
set -euo pipefail

manifest_url='{{MANIFEST_URL}}'
undo=false
[[ "${1:-}" == "--undo" ]] && undo=true
[[ "$#" -le 1 ]] || { echo "Unknown option: ${2:-}" >&2; exit 2; }

target_user="${SUDO_USER:-${USER:-}}"
target_home="${BGA_CODEX_ENV_HOME:-}"
if [[ -z "$target_home" && -n "$target_user" ]]; then
  target_home="$(getent passwd "$target_user" 2>/dev/null | cut -d: -f6 || true)"
fi
target_home="${target_home:-$HOME}"
codex_home="${CODEX_HOME:-${target_home}/.codex}"
config_dir="${BGA_CODEX_CONFIG_DIR:-/etc/codex}"
skill_dir="${codex_home}/skills/bga-connections"
managed_block_start="# BEGIN BG Agents AI Gateway managed block"
managed_block_end="# END BG Agents AI Gateway managed block"

require_root() {
  [[ -n "${BGA_CODEX_CONFIG_DIR:-}" || "${EUID}" -eq 0 ]] || { echo "Run with sudo so the installer can write ${config_dir}." >&2; exit 1; }
}

remove_managed_block() {
  local profile="$1"
  local temp
  [[ -f "$profile" ]] || return 0
  temp="$(mktemp)"
  awk -v start="$managed_block_start" -v finish="$managed_block_end" '
    $0 == start { skipping = 1; next }
    skipping && $0 == finish { skipping = 0; next }
    !skipping { print }
  ' "$profile" >"$temp"
  cat "$temp" >"$profile"
  rm -f "$temp"
}

install_shell_sources() {
  local login_shell profile
  login_shell="$(getent passwd "$target_user" 2>/dev/null | cut -d: -f7 || true)"
  for profile in "${target_home}/.bashrc" "${target_home}/.zshrc"; do
    if [[ ! -f "$profile" ]]; then
      case "$profile:$login_shell" in
        */.bashrc:*bash|*/.zshrc:*zsh) : >"$profile" ;;
        *) continue ;;
      esac
    fi
    remove_managed_block "$profile"
    printf '\n%s\n%s\n%s\n' \
      "$managed_block_start" \
      '[ -f "$HOME/.config/bg-ai-gateway/env.sh" ] && . "$HOME/.config/bg-ai-gateway/env.sh"' \
      "$managed_block_end" >>"$profile"
  done
}

write_codex_env_key() {
  local env_file="${codex_home}/.env"
  local temp
  [[ ! -L "$env_file" ]] || { echo "Refusing to replace symlink ${env_file}." >&2; return 1; }
  install -d -m 0700 "$codex_home"
  temp="$(mktemp "${codex_home}/.env.tmp.XXXXXX")"
  if [[ -f "$env_file" ]] && ! awk '!/^[[:space:]]*(export[[:space:]]+)?BG_AI_GATEWAY_API_KEY[[:space:]]*=/' "$env_file" >"$temp"; then
    rm -f "$temp"
    return 1
  fi
  printf 'BG_AI_GATEWAY_API_KEY=%s\n' "$api_key" >>"$temp"
  chmod 0600 "$temp"
  if ! mv -f "$temp" "$env_file"; then
    rm -f "$temp"
    return 1
  fi
}

remove_codex_env_key() {
  local env_file="${codex_home}/.env"
  local temp
  [[ -f "$env_file" ]] || return 0
  [[ ! -L "$env_file" ]] || { echo "Refusing to modify symlink ${env_file}." >&2; return 1; }
  temp="$(mktemp "${codex_home}/.env.tmp.XXXXXX")"
  if ! awk '!/^[[:space:]]*(export[[:space:]]+)?BG_AI_GATEWAY_API_KEY[[:space:]]*=/' "$env_file" >"$temp"; then
    rm -f "$temp"
    return 1
  fi
  if [[ -s "$temp" ]]; then
    chmod 0600 "$temp"
    if ! mv -f "$temp" "$env_file"; then
      rm -f "$temp"
      return 1
    fi
  else
    rm -f "$temp" "$env_file"
  fi
}

remove_api_key_sources() {
  local profile
  for profile in "${target_home}/.bashrc" "${target_home}/.zshrc"; do
    remove_managed_block "$profile"
  done
  remove_codex_env_key
  rm -f \
    "${target_home}/.config/bg-ai-gateway/env.sh" \
    "${target_home}/.config/environment.d/bg-ai-gateway.conf" \
    "${target_home}/.config/fish/conf.d/bg-ai-gateway.fish"
}

remove_install() {
  require_root
  local config_path="${config_dir}/config.toml"
  local config_backup="${config_path}.bga-backup"
  if [[ -f "$config_path" ]] && grep -q "BG Agents AI Gateway managed config" "$config_path"; then rm -f "$config_path"; fi
  if [[ ! -e "$config_path" && -f "$config_backup" ]]; then mv "$config_backup" "$config_path"; fi
  rm -rf "$skill_dir"
  remove_api_key_sources
  echo "Removed BG AI Gateway Codex setup."
  echo "Restart the shell and Codex, or run: unset BG_AI_GATEWAY_API_KEY BG_AI_GATEWAY_BASE_URL ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN"
}

[[ "$undo" == true ]] && { remove_install; exit 0; }
require_root

manifest="$(curl -fsSL "$manifest_url")"
read_manifest() { python3 -c 'import json,sys; value=json.load(sys.stdin); print(value[sys.argv[1]])' "$1"; }
version="$(printf '%s' "$manifest" | read_manifest version)"
package_sha="$(printf '%s' "$manifest" | read_manifest sha256)"
gateway_base_url="$(printf '%s' "$manifest" | read_manifest gatewayBaseUrl)"
package_url="${manifest_url%/manifest.json}/versions/${version}/package.zip"
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([+-][0-9A-Za-z.-]+)?$ && "$package_sha" =~ ^[0-9a-f]{64}$ && -n "$gateway_base_url" ]] || { echo "Invalid BG AI Gateway package manifest." >&2; exit 1; }

api_key="${BG_AI_GATEWAY_API_KEY:-}"
stored_key="${target_home}/.config/environment.d/bg-ai-gateway.conf"
[[ -n "$api_key" || ! -f "$stored_key" ]] || api_key="$(sed -n 's/^BG_AI_GATEWAY_API_KEY=//p' "$stored_key" | head -1)"
if [[ -n "$api_key" && -t 1 ]]; then
  reply=""
  read -r -p "Keep the existing BG AI Gateway API key? [Y/n] " reply </dev/tty || true
  [[ "$reply" =~ ^([nN]|no|NO)$ ]] && api_key=""
fi
if [[ -z "$api_key" ]]; then
  [[ -t 1 ]] || { echo "API key is required when the installer runs without a terminal." >&2; exit 1; }
  read -r -s -p "BG AI Gateway API key: " api_key </dev/tty
  echo >/dev/tty
fi
[[ -n "$api_key" ]] || { echo "API key is required." >&2; exit 1; }
[[ "$api_key" != *$'\n'* && "$api_key" != *$'\r'* ]] || { echo "API key must not contain a newline." >&2; exit 1; }
if ! curl -fsS -o /dev/null \
  -H "Authorization: Bearer ${api_key}" \
  "${gateway_base_url%/}/v1/models"; then
  echo "BG AI Gateway API key validation failed. Verify the key and Gateway connectivity, then try again." >&2
  exit 1
fi

temp_root="$(mktemp -d)"
cleanup() { rm -rf "$temp_root"; }
trap cleanup EXIT
curl -fsSL "$package_url" -o "$temp_root/package.zip"
echo "${package_sha}  $temp_root/package.zip" | sha256sum -c - >/dev/null
python3 - "$temp_root/package.zip" "$temp_root/stage" "$skill_dir" <<'PY'
import errno, os, pathlib, shutil, sys, zipfile
archive, stage, target = map(pathlib.Path, sys.argv[1:])
with zipfile.ZipFile(archive) as value:
  for info in value.infolist():
    name=pathlib.PurePosixPath(info.filename)
    if name.is_absolute() or '..' in name.parts or (info.external_attr >> 16) & 0o170000 == 0o120000: raise SystemExit('Unsafe BG AI Gateway package path.')
  value.extractall(stage)
source=stage/'bga-connections'
if not all((source/item).is_file() for item in ('SKILL.md','bga-connections.py','package.json')): raise SystemExit('BG AI Gateway package is incomplete.')
target.parent.mkdir(parents=True, exist_ok=True); backup=target.with_name(target.name+'.backup')
if backup.exists(): shutil.rmtree(backup)
if target.exists(): os.replace(target, backup)
try:
  try: os.replace(source, target)
  except OSError as error:
    if error.errno != errno.EXDEV: raise
    try: shutil.copytree(source, target)
    except Exception:
      if target.exists(): shutil.rmtree(target)
      raise
except Exception:
  if backup.exists() and not target.exists(): os.replace(backup,target)
  raise
if backup.exists(): shutil.rmtree(backup)
PY

install -d -m 0755 "$config_dir"
config_path="${config_dir}/config.toml"
config_backup="${config_path}.bga-backup"
if [[ -f "$config_path" ]] && ! grep -q "BG Agents AI Gateway managed config" "$config_path"; then
  [[ ! -e "$config_backup" ]] || { echo "Refusing to replace ${config_path} because ${config_backup} already exists." >&2; exit 1; }
  cp -p "$config_path" "$config_backup"
fi
tmp_config="$temp_root/config.toml"
cat >"$tmp_config" <<EOF
# BG Agents AI Gateway managed config
model_provider = "bg_ai_gateway"

[model_providers.bg_ai_gateway]
name = "BG AI Gateway"
base_url = "${gateway_base_url%/}/codex/v1"
wire_api = "responses"
supports_websockets = false

[model_providers.bg_ai_gateway.auth]
command = "/bin/sh"
args = ["-c", "printenv \"\$1\"", "bga-codex-auth", "BG_AI_GATEWAY_API_KEY"]
EOF
install -m 0644 "$tmp_config" "$config_path"
install -d -m 0700 "${target_home}/.config/bg-ai-gateway" "${target_home}/.config/environment.d"
printf 'export BG_AI_GATEWAY_API_KEY=%q\nexport BG_AI_GATEWAY_BASE_URL=%q\nexport ANTHROPIC_BASE_URL=%q\nexport ANTHROPIC_AUTH_TOKEN=%q\n' \
  "$api_key" "$gateway_base_url" "$gateway_base_url" "$api_key" >"${target_home}/.config/bg-ai-gateway/env.sh"
printf 'BG_AI_GATEWAY_API_KEY=%s\nBG_AI_GATEWAY_BASE_URL=%s\nANTHROPIC_BASE_URL=%s\nANTHROPIC_AUTH_TOKEN=%s\n' \
  "$api_key" "$gateway_base_url" "$gateway_base_url" "$api_key" >"${target_home}/.config/environment.d/bg-ai-gateway.conf"
chmod 0600 "${target_home}/.config/bg-ai-gateway/env.sh" "${target_home}/.config/environment.d/bg-ai-gateway.conf"
write_codex_env_key
install_shell_sources
if [[ "$EUID" -eq 0 && -n "$target_user" && "$target_user" != root ]]; then
  chown -R "${target_user}:" "$codex_home" "${target_home}/.config/bg-ai-gateway" "${target_home}/.config/environment.d" 2>/dev/null || true
  chown "${target_user}:" "${target_home}/.bashrc" "${target_home}/.zshrc" 2>/dev/null || true
fi
echo "Installed BG AI Gateway package ${version}. Open a new terminal, then fully exit and relaunch Codex."
