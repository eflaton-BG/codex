#!/usr/bin/env python3
"""Wrapper for the installed Agent Notes Node CLI."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

USAGE = """Usage:
  python codex_skills_library/agent-notes/scripts/agent_notes_cli.py health
  python codex_skills_library/agent-notes/scripts/agent_notes_cli.py projects list
  python codex_skills_library/agent-notes/scripts/agent_notes_cli.py projects search <query>
  python codex_skills_library/agent-notes/scripts/agent_notes_cli.py projects create [name]
  python codex_skills_library/agent-notes/scripts/agent_notes_cli.py projects rename <id> <name>
  python codex_skills_library/agent-notes/scripts/agent_notes_cli.py projects delete <id>
  python codex_skills_library/agent-notes/scripts/agent_notes_cli.py notes list <project_id>
  python codex_skills_library/agent-notes/scripts/agent_notes_cli.py notes search <project_id> <query>
  python codex_skills_library/agent-notes/scripts/agent_notes_cli.py notes create <project_id> [title]
  python codex_skills_library/agent-notes/scripts/agent_notes_cli.py notes get <note_id>
  python codex_skills_library/agent-notes/scripts/agent_notes_cli.py notes update <note_id> [--title <title>] [--description <description>]
  python codex_skills_library/agent-notes/scripts/agent_notes_cli.py notes delete <note_id>
"""


def resolve_agent_notes_cli_path() -> Path:
    candidate = Path(__file__).resolve().with_name("agent_notes_cli.mjs")
    if candidate.exists():
        return candidate
    raise ValueError(
        f"Installed skill is incomplete: missing bundled agent_notes_cli.mjs at {candidate}"
    )


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if any(arg in {"--help", "-h"} for arg in args):
        print(USAGE, end="")
        return 0

    cli_path = resolve_agent_notes_cli_path()
    command = ["node", str(cli_path), *args]
    completed = subprocess.run(
        command,
        capture_output=True,
        check=False,
        cwd=str(cli_path.parents[1]),
        encoding="utf-8",
        env=None,
    )
    if completed.returncode != 0:
        message = (
            completed.stderr.strip()
            or completed.stdout.strip()
            or f"Agent Notes CLI exited with code {completed.returncode}."
        )
        print(f"agent-notes error: {message}", file=sys.stderr)
        return 1

    if completed.stdout:
        print(completed.stdout, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
