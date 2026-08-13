#!/usr/bin/env bash
set -euo pipefail

manifest_url='{{MANIFEST_URL}}'
undo=false
[[ "${1:-}" == "--undo" ]] && undo=true
[[ "$#" -le 1 ]] || { echo "Unknown option: ${2:-}" >&2; exit 2; }
[[ "$(uname -s)" == "Darwin" || -n "${BGA_MACOS_INSTALL_TEST:-}" ]] || {
  echo "This installer supports macOS only." >&2
  exit 1
}

target_user="${SUDO_USER:-${USER:-}}"
target_home="${BGA_CODEX_ENV_HOME:-}"
if [[ -z "$target_home" && -n "$target_user" ]]; then
  home_record="$(/usr/bin/dscl . -read "/Users/${target_user}" NFSHomeDirectory 2>/dev/null || true)"
  target_home="${home_record#NFSHomeDirectory: }"
fi
if [[ -z "$target_home" ]]; then
  if [[ "${EUID}" -ne 0 || "$target_user" == "$(id -un)" ]]; then
    target_home="$HOME"
  else
    echo "Unable to determine the target user's home directory." >&2
    exit 1
  fi
fi

codex_home="${CODEX_HOME:-${target_home}/.codex}"
config_dir="${BGA_CODEX_CONFIG_DIR:-/etc/codex}"
skill_dir="${codex_home}/skills/bga-connections"
env_dir="${target_home}/.config/bg-ai-gateway"
env_file="${env_dir}/env.sh"
api_key_file="${env_dir}/api-key"
launch_agent_label="com.berkshiregrey.bg-ai-gateway-env"
launch_agent_dir="${target_home}/Library/LaunchAgents"
launch_agent_file="${launch_agent_dir}/${launch_agent_label}.plist"
managed_block_start="# BEGIN BG Agents AI Gateway managed block"
managed_block_end="# END BG Agents AI Gateway managed block"
installing_dir=""

require_root() {
  [[ -n "${BGA_CODEX_CONFIG_DIR:-}" || "${EUID}" -eq 0 ]] || {
    echo "Run with sudo so the installer can write ${config_dir}." >&2
    exit 1
  }
}

target_uid() {
  id -u "$target_user"
}

target_group() {
  id -gn "$target_user"
}

target_shell() {
  local shell_record
  shell_record="$(/usr/bin/dscl . -read "/Users/${target_user}" UserShell 2>/dev/null || true)"
  printf '%s' "${shell_record#UserShell: }"
}

launchctl_for_target_user() {
  local uid
  uid="$(target_uid)"
  if [[ "${EUID}" -eq 0 && "$target_user" != root ]]; then
    /bin/launchctl asuser "$uid" /bin/launchctl "$@"
  else
    /bin/launchctl "$@"
  fi
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
  local profiles=()
  login_shell="$(target_shell)"
  case "$login_shell" in
    *zsh) profiles=("${target_home}/.zshrc") ;;
    *bash) profiles=("${target_home}/.bash_profile" "${target_home}/.bashrc") ;;
    *) profiles=("${target_home}/.profile") ;;
  esac
  for profile in "${profiles[@]}"; do
    [[ -f "$profile" ]] || : >"$profile"
    remove_managed_block "$profile"
    printf '\n%s\n%s\n%s\n' \
      "$managed_block_start" \
      '[ -f "$HOME/.config/bg-ai-gateway/env.sh" ] && . "$HOME/.config/bg-ai-gateway/env.sh"' \
      "$managed_block_end" >>"$profile"
  done
}

remove_shell_sources() {
  local profile
  for profile in \
    "${target_home}/.zshrc" \
    "${target_home}/.bash_profile" \
    "${target_home}/.bashrc" \
    "${target_home}/.profile"; do
    remove_managed_block "$profile"
  done
}

install_launch_agent() {
  local uid
  uid="$(target_uid)"
  /usr/bin/install -d -m 0755 "$launch_agent_dir"
  cat >"$launch_agent_file" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.berkshiregrey.bg-ai-gateway-env</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-c</string>
    <string>[ -f "$HOME/.config/bg-ai-gateway/env.sh" ] &amp;&amp; . "$HOME/.config/bg-ai-gateway/env.sh" &amp;&amp; /bin/launchctl setenv BG_AI_GATEWAY_API_KEY "$BG_AI_GATEWAY_API_KEY" &amp;&amp; /bin/launchctl setenv BG_AI_GATEWAY_BASE_URL "$BG_AI_GATEWAY_BASE_URL" &amp;&amp; /bin/launchctl setenv ANTHROPIC_BASE_URL "$ANTHROPIC_BASE_URL" &amp;&amp; /bin/launchctl setenv ANTHROPIC_AUTH_TOKEN "$ANTHROPIC_AUTH_TOKEN"</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
</dict>
</plist>
PLIST
  chmod 0644 "$launch_agent_file"
  if [[ "${EUID}" -eq 0 && "$target_user" != root ]]; then
    chown "$(target_user_and_group)" "$launch_agent_dir" "$launch_agent_file"
  fi

  /bin/launchctl bootout "gui/${uid}" "$launch_agent_file" >/dev/null 2>&1 || true
  if ! /bin/launchctl bootstrap "gui/${uid}" "$launch_agent_file" >/dev/null 2>&1; then
    echo "Warning: the Codex Desktop environment will load at the next macOS login." >&2
  fi
  /bin/launchctl kickstart -k "gui/${uid}/${launch_agent_label}" >/dev/null 2>&1 || true
  launchctl_for_target_user setenv BG_AI_GATEWAY_API_KEY "$api_key" >/dev/null 2>&1 || true
  launchctl_for_target_user setenv BG_AI_GATEWAY_BASE_URL "$gateway_base_url" >/dev/null 2>&1 || true
  launchctl_for_target_user setenv ANTHROPIC_BASE_URL "$gateway_base_url" >/dev/null 2>&1 || true
  launchctl_for_target_user setenv ANTHROPIC_AUTH_TOKEN "$api_key" >/dev/null 2>&1 || true
}

remove_launch_agent() {
  local uid
  uid="$(target_uid)"
  /bin/launchctl bootout "gui/${uid}" "$launch_agent_file" >/dev/null 2>&1 || true
  launchctl_for_target_user unsetenv BG_AI_GATEWAY_API_KEY >/dev/null 2>&1 || true
  launchctl_for_target_user unsetenv BG_AI_GATEWAY_BASE_URL >/dev/null 2>&1 || true
  launchctl_for_target_user unsetenv ANTHROPIC_BASE_URL >/dev/null 2>&1 || true
  launchctl_for_target_user unsetenv ANTHROPIC_AUTH_TOKEN >/dev/null 2>&1 || true
  rm -f "$launch_agent_file"
}

target_user_and_group() {
  printf '%s:%s' "$target_user" "$(target_group)"
}

remove_install() {
  require_root
  local config_path="${config_dir}/config.toml"
  local config_backup="${config_path}.bga-backup"
  if [[ -f "$config_path" ]] && grep -q "BG Agents AI Gateway managed config" "$config_path"; then
    rm -f "$config_path"
  fi
  if [[ ! -e "$config_path" && -f "$config_backup" ]]; then
    mv "$config_backup" "$config_path"
  fi
  remove_launch_agent
  rm -rf "$skill_dir"
  remove_shell_sources
  rm -rf "$env_dir"
  echo "Removed BG AI Gateway Codex setup."
  echo "Open a new terminal and fully exit and relaunch Codex."
}

[[ "$undo" == true ]] && { remove_install; exit 0; }
require_root

manifest="$(curl -fsSL "$manifest_url")"
read_manifest() {
  printf '%s' "$manifest" | /usr/bin/plutil -extract "$1" raw -o - -
}
version="$(read_manifest version)"
package_sha="$(read_manifest sha256)"
gateway_base_url="$(read_manifest gatewayBaseUrl)"
package_url="${manifest_url%/manifest.json}/versions/${version}/package.zip"
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([+-][0-9A-Za-z.-]+)?$ ]] || {
  echo "Invalid BG AI Gateway package version." >&2
  exit 1
}
[[ "$package_sha" =~ ^[0-9a-f]{64}$ && -n "$gateway_base_url" ]] || {
  echo "Invalid BG AI Gateway package manifest." >&2
  exit 1
}

api_key="${BG_AI_GATEWAY_API_KEY:-}"
if [[ -z "$api_key" && -f "$api_key_file" ]]; then
  api_key="$(cat "$api_key_file")"
fi
if [[ -n "$api_key" && -t 1 ]]; then
  reply=""
  read -r -p "Keep the existing BG AI Gateway API key? [Y/n] " reply </dev/tty || true
  [[ "$reply" =~ ^([nN]|no|NO)$ ]] && api_key=""
fi
if [[ -z "$api_key" ]]; then
  [[ -t 1 ]] || {
    echo "API key is required when the installer runs without a terminal." >&2
    exit 1
  }
  read -r -s -p "BG AI Gateway API key: " api_key </dev/tty
  echo >/dev/tty
fi
[[ -n "$api_key" ]] || { echo "API key is required." >&2; exit 1; }
[[ "$api_key" != *$'\n'* && "$api_key" != *$'\r'* ]] || {
  echo "API key must not contain a newline." >&2
  exit 1
}
if ! curl -fsS -o /dev/null \
  -H "Authorization: Bearer ${api_key}" \
  "${gateway_base_url%/}/v1/models"; then
  echo "BG AI Gateway API key validation failed. Verify the key and Gateway connectivity, then try again." >&2
  exit 1
fi

temp_root="$(mktemp -d)"
cleanup() {
  rm -rf "$temp_root"
  [[ -z "$installing_dir" ]] || rm -rf "$installing_dir"
}
trap cleanup EXIT
curl -fsSL "$package_url" -o "$temp_root/package.zip"
actual_sha="$(/usr/bin/shasum -a 256 "$temp_root/package.zip" | awk '{print $1}')"
[[ "$actual_sha" == "$package_sha" ]] || {
  echo "BG AI Gateway package checksum mismatch." >&2
  exit 1
}

archive_entries="$(/usr/bin/unzip -Z1 "$temp_root/package.zip")"
while IFS= read -r entry; do
  case "$entry" in
    /*|../*|*/../*|*/..|*\\*)
      echo "Unsafe BG AI Gateway package path." >&2
      exit 1
      ;;
  esac
done <<<"$archive_entries"

/usr/bin/ditto -x -k "$temp_root/package.zip" "$temp_root/stage"
if /usr/bin/find "$temp_root/stage" -type l -print -quit | /usr/bin/grep -q .; then
  echo "BG AI Gateway package contains an unsupported symbolic link." >&2
  exit 1
fi
source_dir="$temp_root/stage/bga-connections"
for required in SKILL.md bga-connections.py package.json; do
  [[ -f "${source_dir}/${required}" ]] || {
    echo "BG AI Gateway package is incomplete." >&2
    exit 1
  }
done

/usr/bin/install -d -m 0755 "$(dirname "$skill_dir")"
backup_dir="${skill_dir}.backup"
installing_dir="${skill_dir}.installing.$$"
rm -rf "$backup_dir" "$installing_dir"
/usr/bin/ditto "$source_dir" "$installing_dir"
if [[ -e "$skill_dir" ]]; then
  mv "$skill_dir" "$backup_dir"
fi
if ! mv "$installing_dir" "$skill_dir"; then
  [[ ! -e "$backup_dir" ]] || mv "$backup_dir" "$skill_dir"
  exit 1
fi
installing_dir=""
rm -rf "$backup_dir"

/usr/bin/install -d -m 0755 "$config_dir"
config_path="${config_dir}/config.toml"
config_backup="${config_path}.bga-backup"
if [[ -f "$config_path" ]] && ! grep -q "BG Agents AI Gateway managed config" "$config_path"; then
  [[ ! -e "$config_backup" ]] || {
    echo "Refusing to replace ${config_path} because ${config_backup} already exists." >&2
    exit 1
  }
  cp -p "$config_path" "$config_backup"
fi
cat >"$temp_root/config.toml" <<EOF
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
/usr/bin/install -m 0644 "$temp_root/config.toml" "$config_path"

/usr/bin/install -d -m 0700 "$env_dir"
/usr/bin/install -m 0600 /dev/null "$api_key_file"
printf '%s' "$api_key" >"$api_key_file"
/usr/bin/install -m 0600 /dev/null "$env_file"
printf '%s\n' \
  'export BG_AI_GATEWAY_API_KEY="$(cat "$HOME/.config/bg-ai-gateway/api-key")"' \
  "export BG_AI_GATEWAY_BASE_URL=$(printf '%q' "$gateway_base_url")" \
  "export ANTHROPIC_BASE_URL=$(printf '%q' "$gateway_base_url")" \
  'export ANTHROPIC_AUTH_TOKEN="$BG_AI_GATEWAY_API_KEY"' >"$env_file"
install_shell_sources

if [[ "${EUID}" -eq 0 && "$target_user" != root ]]; then
  owner="$(target_user_and_group)"
  chown -R "$owner" "$skill_dir" "$env_dir"
  chown "$owner" "$(dirname "$skill_dir")" "$codex_home" 2>/dev/null || true
  for profile in \
    "${target_home}/.zshrc" \
    "${target_home}/.bash_profile" \
    "${target_home}/.bashrc" \
    "${target_home}/.profile"; do
    [[ ! -e "$profile" ]] || chown "$owner" "$profile"
  done
fi

install_launch_agent
echo "Installed BG AI Gateway package ${version}."
echo "Open a new terminal, then fully exit and relaunch Codex."
