---
name: implement-ticket
description: Use when the user wants a Jira-backed engineering ticket implemented end-to-end in the local workspace, including ticket context lookup, repo/package discovery, guarded branch workflow, targeted validation, and existing PR updates.
---

# Implement Ticket

Use this skill when the user asks to implement a Jira ticket or shares a Jira key or Jira URL and expects code changes, validation, and possibly branch or existing PR work.

## Workflow

1. Resolve the Jira key and fetch the ticket with Atlassian MCP.
2. Summarize the ticket goal, constraints, status, and any linked context that affects implementation.
3. Locate the affected repo and package in the current workspace before editing.
4. Before any branch change, inspect the target repo worktree for uncommitted changes.
5. If a branch change is needed and the worktree is dirty, stop and wait for explicit user permission before continuing.
6. If a branch change is needed and the worktree is clean, check out `2204_devel` first and update it from remote before creating a new branch.
7. Use branch names in the form `bugfix/<TICKET>-<short-kebab>` or `feature/<TICKET>-<short-kebab>`, and get user confirmation on the exact branch name before creating it.
8. Implement the smallest defensible code change that satisfies the ticket.
9. Run targeted validation for the touched area first.
10. Run required formatting, commit only the intended changes, and push the branch when appropriate.
11. Do not open a new PR without explicit user approval unless the user has already clearly asked for a PR.
12. If a PR already exists for the branch, updating that PR is allowed as part of the normal implementation flow.

## Git and Branch Rules

- Do not switch branches blindly. Check `git status --short --branch` first.
- If uncommitted work exists and a branch switch is required, stop and ask for permission.
- Do not revert unrelated user changes.
- Prefer merge-based sync with remote unless the user explicitly asks for rebase.
- Never use force-push.
- Do not open a new PR without explicit approval unless the user has already stated that they want a PR.
- Updating an existing PR is allowed.

## Validation Rules

- Prefer narrow unit or integration tests that directly cover the ticket behavior.
- If test failures appear to come from build issues, stale installs, missing imports, missing generated artifacts, environment setup, or similar infrastructure problems, do not start patching code to work around them.
- In that case, give the user the exact failing command and the relevant logs, then wait for the user to fix the environment or approve the next step.
- When appropriate, suggest rebuilding the affected package with `bgbuild --packages-select <package_name>`.
- Only continue with code changes after the build or environment issue is resolved or the user explicitly redirects you.

## Breakpack-Specific Notes

- Run `ros2` commands inside the workspace container.
- Run Jira and GitHub commands on the host.
- For PR prep in breakpack repos, run `ruff format` and `isort .` in the repo root before creating or updating the PR.
- If host formatting tools are unavailable, run them in `colcon_ws-workspace-1`.

## Output Expectations

- Keep the user updated with short status messages as work progresses.
- When mentioning Jira tickets, include the summary and direct Jira link.
- When a PR is created or updated, include the direct PR URL.
- If blocked, say exactly what is blocked, what command failed, and what you need from the user.
