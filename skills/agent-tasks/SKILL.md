---
name: "agent-tasks"
description: "Use the local Agent Tasks CLI to inspect and update the Agent Tasks app tree, steps, and comments."
---

# Agent Tasks

Use this skill for local `agent_tasks` data through the app CLI instead of editing the SQLite database directly.

CLI:
```bash
python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py --help
python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py health
python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py show 48
python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py search --query "CLI" --active-only
python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py tree --root-id 108 --active-only
python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py move 12 --parent-id 5 --insert-index 0
```

Rules:
- Prefer the CLI for creating, updating, and deleting nodes, steps, and comments.
- Use `health` first if the local Agent Tasks service may not be running.
- Prefer targeted lookup over broad discovery: use `show <node_id>` when you already have an id, `search` to find likely matches, and `tree --root-id <id> --active-only` when you need one branch rather than the full tree.
- Use `search` with its default active-work filtering for context discovery unless you explicitly need completed or aborted work too. Here "active" means node statuses `new`, `ready`, `blocked`, `working`, or `review`; `complete` and `aborted` are excluded. Add `--all-statuses` or `--statuses <csv>` only when the missing context is likely outside active work.
- `tree` does not apply the active filter by default; pass `--active-only` when you want the same completed/aborted exclusion behavior for a subtree.
- Do not dump the full tree into agent context unless the task truly requires broad discovery. When you already know the task id or likely folder, fetch only the specific node or subtree you need and exclude unrelated completed/aborted branches by default.
- When talking to the user about task steps, always refer to steps by their task-local step numbering/order, not by the globally unique step id.
- When creating or rewriting tasks/steps/comments for future work, assume the next worker is a cold-start agent with no access to the prior chat. Write summaries and steps so they stand on their own, and put concrete context pointers in comments (links, source docs, systems to inspect, blocking assumptions) instead of vague references like "this effort" or "as discussed above."
- Before creating a new task, scan the tree for an existing task that already covers the same work. Reuse/update the existing task when it is clearly the same request instead of creating parallel duplicates.
- Folder placement matters. Inspect the current tree/subfolders first and put new tasks in the most specific relevant folder rather than dropping them into a broad top-level bucket where they may be overlooked.
- Use comments for source context, durable links, evolving notes, and cold-start handoff material.
- If a request is still fuzzy, speculative, or not implementation-ready, create a planning/definition task instead of pretending it is already a concrete build task.

Creating tasks:
- Write summaries so a cold-start worker can tell what the task is for without reading prior chat.
- Write steps as concrete worker actions or expected outputs, not vague reminders.
- Use comments to capture the source request, links, systems to inspect, important constraints, and any assumptions that would otherwise be trapped in chat history.
- If the right split is unclear, prefer a smaller number of well-scoped tasks and ask the user before creating a sprawling backlog blob.

Working a task:
- Start by reading the task's summary, existing steps, and recent comments so you inherit current context before changing anything.
- When you begin substantive work on a task, set the task status to `working`. Leave untouched tasks as `new` or `ready`.
- Keep step state current as you work: mark the active step `working`, mark it `complete` only after the work for that step is actually done and validated, and use `blocked` when an external dependency or unresolved issue is preventing progress.
- Never leave a task in `working` when you are done for this turn. End in `review` when waiting on user review, `complete` when the user has confirmed it is done, or `blocked` only when something other than a user decision/action is preventing progress.
- If you complete implementation but are waiting on user review or confirmation, set the task status to `review` rather than `complete`.
- Only mark the task `complete` after the user has reviewed or explicitly confirmed completion.
- Use `aborted` only when the task is intentionally dropped or replaced, not as a synonym for blocked.
- For partial requests like "do step 1 only", update only the relevant step(s) and leave the remaining steps untouched.

Comments:
- Use comments as the durable work log for task-specific context, not as casual chat.
- Add comments when you discover important context, start a meaningful work slice, hit a blocker, or finish a step with validation results.
- Include high-signal facts in comments: decisions made, files changed, commands/tests run, outputs or artifacts produced, blockers, and anything the next agent or the user would need to continue safely.
- Prefer multiple focused comments over one giant summary dump when the work naturally happened in stages; this keeps cold-start handoff readable and makes it easier to find the one slice that matters.
- Prefer adding a new comment over rewriting history. Update or delete a comment only to correct the same comment.
- Do not rely on summary text for live progress notes; put evolving context in comments.
- When any free-text CLI argument contains shell-sensitive characters such as backticks, `$`, quotes, or newlines, do not pass the raw body through a shell-quoted one-liner. Use a safe argument list via Python `subprocess.run([...])`, stdin, or a temp file so the shell cannot eat or mutate the text before it reaches the CLI.

Common commands:
```bash
python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py show 124
python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py search --query "slack agent"
python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py search --query "workflow" --root-id 108 --all-statuses
python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py tree --root-id 108 --active-only
python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py move 12 --parent-id 5 --insert-index 0
python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py move 12 --root --insert-index 1
python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py create-folder --summary "New Folder"
python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py create-task --summary "New Task"
python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py create-task --parent-id 12 --summary "Child Task"
python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py update 12 --summary "Renamed task" --status working --priority urgent
python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py update 12 --status review
python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py add-step 12 "Investigate issue"
python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py update-step 7 --status working
python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py update-step 7 --status complete --position 1
python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py update-step 7 --status blocked
python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py add-comment 12 "Found root cause in file X; validating with pytest now."
python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py update-comment 5 "Agent follow-up updated"
python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py delete-comment 5
python codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py delete 12
```

Safe comment update pattern when the body may contain backticks or other shell metacharacters:
```bash
python - <<'PY'
import subprocess
body = "Step 1 complete. Literal backticks like `foo` stay intact here."
subprocess.run(
    [
        "python",
        "codex_skills_library/agent-tasks/scripts/agent_tasks_cli.py",
        "add-comment",
        "79",
        body,
    ],
    check=True,
)
PY
```

Task states:
- Normal flow is usually `new`/`ready` -> `working` -> `review` -> `complete`.
- Use `blocked` when the whole task cannot currently move forward.
- Folder status is usually less important than task and step status; prioritize keeping task/step state accurate.

Step states:
- `ready`, `working`, and `complete` are the normal UI cycle.
- `blocked` is CLI-only and should be set with `update-step --status blocked`.

Installed skill note:
- The installed `agent-tasks` skill is expected to be self-contained under `$CODEX_HOME/skills/agent-tasks` and carry its bundled Node CLI beside the Python wrapper.
