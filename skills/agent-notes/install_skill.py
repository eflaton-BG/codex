#!/usr/bin/env python3
"""Install the agent-notes skill into $CODEX_HOME/skills as a self-contained bundle."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def copy_file(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)


def resolve_repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def resolve_install_dir() -> Path:
    codex_home = Path(
        os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
    ).expanduser()
    return codex_home / "skills" / "agent-notes"


def main() -> int:
    repo_root = resolve_repo_root()
    install_dir = resolve_install_dir()
    skill_src = repo_root / "codex_skills_library" / "agent-notes"
    app_src = repo_root / "agent_suite" / "agent_notes"

    if install_dir.exists():
        shutil.rmtree(install_dir)

    copy_file(skill_src / "SKILL.md", install_dir / "SKILL.md")
    copy_file(
        skill_src / "agents" / "openai.yaml", install_dir / "agents" / "openai.yaml"
    )
    copy_file(skill_src / "install_skill.py", install_dir / "install_skill.py")
    copy_file(
        skill_src / "scripts" / "agent_notes_cli.py",
        install_dir / "scripts" / "agent_notes_cli.py",
    )
    copy_file(
        app_src / "scripts" / "agent_notes_cli.mjs",
        install_dir / "scripts" / "agent_notes_cli.mjs",
    )
    copy_file(app_src / "service" / "client.js", install_dir / "service" / "client.js")
    copy_file(app_src / "service" / "paths.js", install_dir / "service" / "paths.js")

    print(install_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
