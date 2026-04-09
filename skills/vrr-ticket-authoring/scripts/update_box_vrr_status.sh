#!/bin/bash
set -euo pipefail

usage() {
  /usr/bin/cat >&2 <<'EOF'
Usage:
  update_box_vrr_status.sh inspect <box-file-url-or-id>
  update_box_vrr_status.sh apply <box-file-url-or-id> --entry <marker> <status-line> [--entry <marker> <status-line> ...]
  update_box_vrr_status.sh apply <box-file-url-or-id> --plan-json <plan-json>

Preferred direct-entry format:
  update_box_vrr_status.sh apply <box-file-url-or-id> \
    --entry "##TICKET ..." "TICKET_STATUS: WRITTEN | YYYY-MM-DD | RSPS-1234 | Summary | https://berkshiregrey.atlassian.net/browse/RSPS-1234"

Legacy plan JSON format:
[
  {
    "marker": "##TICKET ...",
    "status_line": "TICKET_STATUS: WRITTEN | YYYY-MM-DD | RSPS-1234 | Summary | https://berkshiregrey.atlassian.net/browse/RSPS-1234"
  }
]
EOF
}

if [[ $# -lt 2 ]]; then
  usage
  exit 2
fi

COMMAND="$1"
INPUT_ARG="$2"
shift 2

PLAN_JSON=""
PLAN_ENTRIES=()

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

case "$COMMAND" in
  inspect)
    if [[ $# -ne 0 ]]; then
      usage
      exit 2
    fi
    ;;
  apply)
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --plan-json)
          if [[ $# -lt 2 ]]; then
            /usr/bin/printf 'Missing value for --plan-json\n' >&2
            exit 2
          fi
          PLAN_JSON="$2"
          shift 2
          ;;
        --entry)
          if [[ $# -lt 3 ]]; then
            /usr/bin/printf 'Each --entry requires <marker> and <status-line>\n' >&2
            exit 2
          fi
          PLAN_ENTRIES+=("$2"$'\n'"$3")
          shift 3
          ;;
        *)
          /usr/bin/printf 'Unknown apply argument: %s\n' "$1" >&2
          usage
          exit 2
          ;;
      esac
    done
    if [[ -n "$PLAN_JSON" && ${#PLAN_ENTRIES[@]} -gt 0 ]]; then
      /usr/bin/printf 'Use either --plan-json or --entry arguments, not both.\n' >&2
      exit 2
    fi
    if [[ -n "$PLAN_JSON" ]]; then
      if [[ ! -f "$PLAN_JSON" ]]; then
        /usr/bin/printf 'Plan JSON not found: %s\n' "$PLAN_JSON" >&2
        exit 2
      fi
    elif [[ ${#PLAN_ENTRIES[@]} -eq 0 ]]; then
      /usr/bin/printf 'apply requires at least one --entry or a --plan-json file.\n' >&2
      exit 2
    fi
    ;;
  *)
    usage
    exit 2
    ;;
esac

HOST_HOME="${HOME:-}"
if [[ -z "$HOST_HOME" ]]; then
  /usr/bin/printf 'HOME is not set.\n' >&2
  exit 1
fi

BOX_TOOLKIT_REPO="${BOX_TOOLKIT_REPO:-$HOST_HOME/devel/mcp-server-box}"
ENV_FILE="$BOX_TOOLKIT_REPO/.env"
AUTH_FILE="$BOX_TOOLKIT_REPO/.auth.oauth"
CACHE_ROOT="/tmp/codex-box-writeback"
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
export HOME="$CACHE_ROOT/home"
export XDG_CACHE_HOME="$HOME/.cache"
export XDG_CONFIG_HOME="$HOME/.config"
export XDG_DATA_HOME="$HOME/.local/share"

cd "$BOX_TOOLKIT_REPO"
set -a
. "$ENV_FILE"
set +a

"$UV_BIN" run python - "$COMMAND" "$BOX_FILE_ID" "${PLAN_JSON:-}" "${PLAN_ENTRIES[@]}" <<'PY'
import json
import pathlib
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile

from box_ai_agents_toolkit import box_file_download, get_oauth_client
from box_sdk_gen.managers.uploads import UploadFileVersionAttributes

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_NS = "http://www.w3.org/XML/1998/namespace"
NS = {"w": W_NS}


def paragraph_text(paragraph):
    return "".join(node.text or "" for node in paragraph.findall(".//w:t", NS)).strip()


def make_text_paragraph(text):
    paragraph = ET.Element(f"{{{W_NS}}}p")
    run = ET.SubElement(paragraph, f"{{{W_NS}}}r")
    text_node = ET.SubElement(run, f"{{{W_NS}}}t")
    text_node.set(f"{{{XML_NS}}}space", "preserve")
    text_node.text = text
    return paragraph


def rebuild_docx(source_path, xml_bytes, output_path):
    with zipfile.ZipFile(source_path, "r") as src, zipfile.ZipFile(output_path, "w") as dst:
        for info in src.infolist():
            data = xml_bytes if info.filename == "word/document.xml" else src.read(info.filename)
            dst.writestr(info, data)


def download_docx(client, file_id, work_dir):
    target = pathlib.Path(work_dir) / f"{file_id}.docx"
    saved_path, _, mime_type = box_file_download(
        client,
        file_id,
        save_file=True,
        save_path=str(target),
    )
    if not saved_path:
        raise RuntimeError("Box download did not return a saved path")
    return pathlib.Path(saved_path), mime_type


def inspect_docx(docx_path):
    with zipfile.ZipFile(docx_path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    paragraphs = root.findall(".//w:body/w:p", NS)
    for index, paragraph in enumerate(paragraphs):
        text = paragraph_text(paragraph)
        if "##TICKET" in text or "TICKET_STATUS" in text or "TICKETUPDATE_STATUS" in text:
            print(f"{index}: {text}")


def apply_statuses(docx_path, plan):
    with zipfile.ZipFile(docx_path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    body = root.find(".//w:body", NS)
    if body is None:
        raise RuntimeError("Unable to find document body in word/document.xml")

    children = list(body)
    paragraph_positions = []
    for idx, child in enumerate(children):
        if child.tag == f"{{{W_NS}}}p":
            paragraph_positions.append((idx, child))

    paragraph_texts = [paragraph_text(paragraph) for _, paragraph in paragraph_positions]
    summary = []
    offset = 0

    for item in plan:
        marker = item["marker"].strip()
        status_line = item["status_line"].strip()

        if status_line in paragraph_texts:
            summary.append({"marker": marker, "status_line": status_line, "result": "already-present"})
            continue

        matches = [idx for idx, text in enumerate(paragraph_texts) if text == marker]
        if len(matches) != 1:
            raise RuntimeError(f"Expected exactly one marker match for: {marker!r}; found {len(matches)}")

        match_idx = matches[0]
        body_insert_idx = paragraph_positions[match_idx][0] + 1 + offset
        body.insert(body_insert_idx, make_text_paragraph(status_line))
        offset += 1
        paragraph_texts.insert(match_idx + 1, status_line)
        paragraph_positions = []
        children = list(body)
        for idx, child in enumerate(children):
            if child.tag == f"{{{W_NS}}}p":
                paragraph_positions.append((idx, child))
        summary.append({"marker": marker, "status_line": status_line, "result": "inserted"})

    xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    output_path = docx_path.with_suffix(".updated.docx")
    rebuild_docx(docx_path, xml_bytes, output_path)
    return output_path, summary


def verify_statuses(docx_path, status_lines):
    with zipfile.ZipFile(docx_path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    paragraph_text_set = {paragraph_text(paragraph) for paragraph in root.findall(".//w:body/w:p", NS)}
    missing = [line for line in status_lines if line not in paragraph_text_set]
    if missing:
        raise RuntimeError(f"Verification failed; missing status lines: {missing}")


def load_plan(plan_path_arg, entry_args):
    if plan_path_arg:
        plan = json.loads(pathlib.Path(plan_path_arg).read_text(encoding="utf-8"))
    else:
        plan = []
        for entry in entry_args:
            try:
                marker, status_line = entry.split("\n", 1)
            except ValueError as exc:
                raise RuntimeError(f"Invalid direct entry payload: {entry!r}") from exc
            plan.append({"marker": marker, "status_line": status_line})
    if not isinstance(plan, list) or not plan:
        raise RuntimeError("Plan must be a non-empty array")
    for item in plan:
        if "marker" not in item or "status_line" not in item:
            raise RuntimeError("Each plan entry must include marker and status_line")
    return plan


def main():
    command = sys.argv[1]
    file_id = sys.argv[2]
    plan_path_arg = sys.argv[3] if len(sys.argv) > 3 else ""
    entry_args = sys.argv[4:]
    client = get_oauth_client()

    with tempfile.TemporaryDirectory(prefix="codex-box-vrr-") as work_dir:
        docx_path, mime_type = download_docx(client, file_id, work_dir)
        if command == "inspect":
            print(f"FILE={docx_path}")
            print(f"MIME={mime_type}")
            inspect_docx(docx_path)
            return

        plan = load_plan(plan_path_arg, entry_args)
        updated_docx_path, summary = apply_statuses(docx_path, plan)
        file_info = client.files.get_file_by_id(file_id)
        with updated_docx_path.open("rb") as handle:
            response = client.uploads.upload_file_version(
                file_id,
                UploadFileVersionAttributes(name=file_info.name),
                handle,
            )
        redownloaded_path, _ = download_docx(client, file_id, work_dir)
        verify_statuses(redownloaded_path, [item["status_line"] for item in plan])
        print(json.dumps({"upload_response_type": type(response).__name__, "summary": summary}, indent=2))


if __name__ == "__main__":
    main()
PY
