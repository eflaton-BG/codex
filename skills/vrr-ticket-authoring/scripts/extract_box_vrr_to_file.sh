#!/bin/bash
set -euo pipefail

usage() {
  /usr/bin/printf 'Usage: %s <box-file-url-or-id> <output-file>\n' "$0" >&2
}

if [[ $# -ne 2 ]]; then
  usage
  exit 2
fi

SCRIPT_DIR="$(/usr/bin/dirname "$0")"
EXTRACTOR="$SCRIPT_DIR/extract_box_vrr.sh"

if [[ ! -x "$EXTRACTOR" ]]; then
  /usr/bin/printf 'Extractor script is not executable: %s\n' "$EXTRACTOR" >&2
  exit 1
fi

exec "$EXTRACTOR" "$1" "$2"
