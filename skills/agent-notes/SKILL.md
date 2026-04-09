---
name: "agent-notes"
description: "Use the local Agent Notes CLI to check service health and list, create, rename, update, and delete Agent Notes projects and notes."
---

# agent-notes

Use this skill for local Agent Notes operations through the Agent Notes CLI instead of editing files or databases directly.

Script:
- `python codex_skills_library/agent-notes/scripts/agent_notes_cli.py --help`

Rules:
- Prefer `health` first if the Agent Notes service may not be running.
- Prefer the CLI for note/project changes instead of manual database edits.

## Typical flow
Check service health:
```bash
python codex_skills_library/agent-notes/scripts/agent_notes_cli.py health
```

List projects:
```bash
python codex_skills_library/agent-notes/scripts/agent_notes_cli.py projects list
```

Search projects:
```bash
python codex_skills_library/agent-notes/scripts/agent_notes_cli.py projects search "agent notes"
```

Create a project:
```bash
python codex_skills_library/agent-notes/scripts/agent_notes_cli.py projects create "Warehouse ideas"
```

Rename a project:
```bash
python codex_skills_library/agent-notes/scripts/agent_notes_cli.py projects rename 12 "Warehouse ideas v2"
```

List notes for a project:
```bash
python codex_skills_library/agent-notes/scripts/agent_notes_cli.py notes list 12
```

Search notes in a project:
```bash
python codex_skills_library/agent-notes/scripts/agent_notes_cli.py notes search 12 "daemon checks"
```

Create a note:
```bash
python codex_skills_library/agent-notes/scripts/agent_notes_cli.py notes create 12 "Daemon checks"
```

Get a note:
```bash
python codex_skills_library/agent-notes/scripts/agent_notes_cli.py notes get 34
```

Update a note:
```bash
python codex_skills_library/agent-notes/scripts/agent_notes_cli.py notes update 34 \
  --title "Daemon checks" \
  --description "Agent Notes daemon is healthy."
```

Delete a note:
```bash
python codex_skills_library/agent-notes/scripts/agent_notes_cli.py notes delete 34
```

Installed skill note:
- The installed `agent-notes` skill is expected to be self-contained under `$CODEX_HOME/skills/agent-notes` and carry its bundled Node CLI beside the Python wrapper.
