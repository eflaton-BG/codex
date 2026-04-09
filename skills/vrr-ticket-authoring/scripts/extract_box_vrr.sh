#!/bin/bash
set -euo pipefail

usage() {
  /usr/bin/printf 'Usage: %s <box-file-url-or-id> [output-file]\n' "$0" >&2
}

if [[ $# -lt 1 || $# -gt 2 ]]; then
  usage
  exit 2
fi

INPUT_ARG="$1"
OUTPUT_FILE="${2:-}"

extract_file_id() {
  local input="$1"
  if [[ "$input" =~ /file/([0-9]+) ]]; then
    /usr/bin/printf '%s\n' "${BASH_REMATCH[1]}"
    return 0
  fi
  if [[ "$input" =~ ^[0-9]+$ ]]; then
    /usr/bin/printf '%s\n' "$input"
    return 0
  fi
  return 1
}

BOX_FILE_ID="$(extract_file_id "$INPUT_ARG")" || {
  /usr/bin/printf 'Unable to determine a Box file ID from: %s\n' "$INPUT_ARG" >&2
  exit 2
}

HOST_HOME="${HOME:-}"
if [[ -z "$HOST_HOME" ]]; then
  /usr/bin/printf 'HOME is not set.\n' >&2
  exit 1
fi

BOX_TOOLKIT_REPO="${BOX_TOOLKIT_REPO:-$HOST_HOME/devel/mcp-server-box}"
ENV_FILE="$BOX_TOOLKIT_REPO/.env"
AUTH_FILE="$BOX_TOOLKIT_REPO/.auth.oauth"
CACHE_ROOT="/tmp/codex-box-extract"
UV_BIN="${UV_BIN:-$(/usr/bin/which uv)}"

if [[ ! -d "$BOX_TOOLKIT_REPO" ]]; then
  /usr/bin/printf 'Box toolkit repo not found: %s\n' "$BOX_TOOLKIT_REPO" >&2
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  /usr/bin/printf 'Missing Box toolkit env file: %s\n' "$ENV_FILE" >&2
  exit 1
fi

if [[ ! -f "$AUTH_FILE" ]]; then
  /usr/bin/printf 'Missing Box toolkit auth file: %s\n' "$AUTH_FILE" >&2
  exit 1
fi

if [[ -z "$UV_BIN" || ! -x "$UV_BIN" ]]; then
  /usr/bin/printf 'Unable to find an executable uv binary.\n' >&2
  exit 1
fi

/usr/bin/mkdir -p \
  "$CACHE_ROOT/uv-cache" \
  "$CACHE_ROOT/home/.cache" \
  "$CACHE_ROOT/home/.config" \
  "$CACHE_ROOT/home/.local/share" \
  "$CACHE_ROOT/files"

export UV_CACHE_DIR="$CACHE_ROOT/uv-cache"
export CACHE_ROOT
export HOME="$CACHE_ROOT/home"
export XDG_CACHE_HOME="$HOME/.cache"
export XDG_CONFIG_HOME="$HOME/.config"
export XDG_DATA_HOME="$HOME/.local/share"

cd "$BOX_TOOLKIT_REPO"
set -a
. "$ENV_FILE"
set +a

"$UV_BIN" run python - "$BOX_FILE_ID" "$OUTPUT_FILE" <<'PY'
import os
import pathlib
import shutil
import sys
import xml.etree.ElementTree as ET
import zipfile

from box_ai_agents_toolkit import box_file_download, box_file_text_extract, get_oauth_client

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W_NS}
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def paragraph_text(paragraph):
    return "".join(node.text or "" for node in paragraph.findall(".//w:t", NS)).strip()


def extract_docx_text(docx_path):
    with zipfile.ZipFile(docx_path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    paragraphs = [
        paragraph_text(paragraph)
        for paragraph in root.findall(".//w:body/w:p", NS)
    ]
    return "\n\n".join(text for text in paragraphs if text)


def extract_local_text(saved_path, mime_type):
    if mime_type == DOCX_MIME or saved_path.suffix.lower() == ".docx":
        return extract_docx_text(saved_path)
    try:
        return saved_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return saved_path.read_text(encoding="utf-8", errors="replace")


def download_latest_file(client, file_id):
    cache_root = pathlib.Path(os.environ["CACHE_ROOT"])
    download_dir = cache_root / "files" / file_id
    if download_dir.exists():
        shutil.rmtree(download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)
    target = download_dir / file_id
    saved_path, _, mime_type = box_file_download(
        client,
        file_id,
        save_file=True,
        save_path=str(target),
    )
    if not saved_path:
        raise RuntimeError("Box download did not return a saved path")
    return pathlib.Path(saved_path), mime_type


file_id = sys.argv[1]
output_arg = sys.argv[2] if len(sys.argv) > 2 else ""
client = get_oauth_client()
saved_path, mime_type = download_latest_file(client, file_id)
content = extract_local_text(saved_path, mime_type)
if not content.strip():
    resp = box_file_text_extract(client, file_id)
    content = resp.get("content", "") if isinstance(resp, dict) else ""

if output_arg:
    output_path = pathlib.Path(output_arg)
    output_path.write_text(content, encoding="utf-8")
    print(output_path)
else:
    print(content, end="")
PY
