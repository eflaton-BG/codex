#!/usr/bin/env python3
"""Wrapper for the installed Agent Tasks Node CLI."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

USAGE = """Usage:
  python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py health
  python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py tree [--root-id <id>] [--statuses <csv>] [--active-only]
  python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py show <node_id>
  python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py search [--query <text>] [--kind <any|task|folder>] [--root-id <id>] [--limit <n>] [--statuses <csv>] [--active-only] [--all-statuses]
  python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py create-folder [--parent-id <id>] [--summary <text>]
  python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py create-task [--parent-id <id>] [--summary <text>]
  python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py move <node_id> [--parent-id <id> | --root] --insert-index <n>
  python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py update <node_id> [--summary <text>] [--status <status>] [--priority <priority>] [--due-at <iso>]
  python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py delete <node_id>
  python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py add-step <node_id> [text]
  python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py update-step <step_id> [--text <text>] [--status <ready|blocked|working|complete>] [--position <n>]
  python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py delete-step <step_id>
  python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py add-comment <node_id> [body]
  python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py update-comment <comment_id> [body]
  python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py delete-comment <comment_id>

Notes:
  --active-only keeps nodes with statuses new, ready, blocked, working, or review,
    and excludes complete/aborted work unless a matching descendant still needs to
    keep a parent folder visible.
  search defaults to active-only filtering; tree only uses it when you pass --active-only.
"""


def resolve_agent_tasks_cli_path() -> Path:
    candidate = Path(__file__).resolve().with_name("agent_tasks_cli.mjs")
    if candidate.exists():
        return candidate
    raise ValueError(
        f"Installed skill is incomplete: missing bundled agent_tasks_cli.mjs at {candidate}"
    )


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if any(arg in {"--help", "-h"} for arg in args):
        print(USAGE, end="")
        return 0

    cli_path = resolve_agent_tasks_cli_path()
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
            or f"Agent Tasks CLI exited with code {completed.returncode}."
        )
        print(f"agent-tasks error: {message}", file=sys.stderr)
        return 1

    if completed.stdout:
        print(completed.stdout, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
