---
name: delegate-task
description: Delegate well-scoped work to Codex subagents with minimal prompt overhead, route routine execution to Luna, complex but bounded execution to Terra, and serious judgment to Sol, then supervise, verify, and close workers. Use when the user explicitly asks for a subagent, delegation, Luna, Terra, Sol, parallel agents, or multi-agent orchestration, or when applicable workspace instructions explicitly require delegation. Do not use merely because a task is large or would benefit from analysis.
---

# Delegate Task

Create compact, decision-complete worker prompts and supervise subagents without duplicating their work.

## Gate Delegation

- Spawn only when the user or applicable instructions explicitly authorize subagents or delegation.
- Read and enforce the active `AGENTS.md` before assigning work.
- Keep immediate blocking work local unless the user explicitly requests that a subagent perform it.
- Delegate one bounded responsibility per worker with a clear output and verification method.
- Never assign overlapping write scopes.

## Route the Model

Use the lowest-cost model that can complete the task reliably:

| Model | Use for |
| --- | --- |
| **Luna** (`gpt-5.6-luna`) | Routine, mechanical, or high-volume execution when actions and verification can be specified exactly. |
| **Terra** (`gpt-5.6-terra`) | Complex multi-step execution with a clear objective, such as difficult debugging or broad codebase navigation, where the worker may choose tactics but not make serious product or architecture judgments. |
| **Sol** (`gpt-5.6-sol`) | Serious judgment: architecture, strategy, ambiguous investigations, high-risk decisions, conflicting evidence, coordinator/integrator roles, or decisions with material consequences if wrong. |

Default to Luna. Escalate only when the task genuinely requires the next model:

- Choose Luna when the prompt can state exact actions plus exact acceptance checks.
- Choose Terra when the goal and acceptance checks are fixed but the execution path must be discovered.
- Choose Sol when the worker must decide what should be done, reconcile ambiguity, or own a consequential judgment.
- Do not route based only on whether the task is read-only or mutating.

## Prepare the Assignment

Specify:

1. One concrete task.
2. Working directory and relevant source identifiers.
3. Authorized write scope, or explicitly state that no writes are allowed.
4. Constraints and approval boundaries.
5. Objective verification checks.
6. Required evidence and final deliverable.

Keep context lean:

- Default `fork_context` to `false`.
- Pass only task-local facts, paths, links, identifiers, and constraints.
- Set `fork_context` to `true` only when conversation history is necessary and cannot be summarized safely.
- Prefer a generated prompt over repeatedly restating orchestration boilerplate.

## Generate the Worker Prompt

Run the bundled script and use its JSON output as the basis for `spawn_agent`:

```bash
/usr/bin/python3 <skill-dir>/scripts/build_worker_prompt.py \
  --model luna \
  --task "Move unresolved direct children from the V1 Jira epic to V2." \
  --workdir "/home/user/workspace" \
  --write-scope "Only the parent field of matching Jira tickets." \
  --constraint "Preserve every other Jira field." \
  --verification "The V1 query returns zero unresolved direct children." \
  --verification "Every moved ticket has the V2 epic as parent." \
  --deliverable "List moved and skipped tickets with verification evidence."
```

The script emits `model`, `reasoning_effort`, `fork_context`, and a compact `message`.

## Spawn and Supervise

1. Discover the multi-agent tools with `tool_search` when they are not already loaded.
2. Spawn with the exact generated model ID. Use medium reasoning unless the task clearly warrants another effort.
3. Follow workspace-specific coordination, worktree, logging, approval, and heartbeat rules.
4. Immediately after spawning any worker, give the user a copy-paste command to follow the coordination log. Use the actual coordination-log path when one is configured; otherwise provide:

   ```bash
   /usr/bin/tail -F /tmp/codex-session-notes/latest.md
   ```

   Provide log-tailing instructions for every delegated run, even when the user did not separately request progress details.
5. Do meaningful non-overlapping coordinator work while workers run.
6. Wait only when the next critical-path action requires the result.
7. Review the worker's evidence rather than repeating the delegated task.
8. Send corrections to the same worker when they remain within its assigned scope.
9. Close completed or abandoned agents promptly.

## Safety

- Treat user authorization as limited to the stated write scope.
- Instruct workers to stop and report concurrent changes, missing permissions, or scope ambiguity.
- Never place secrets in worker prompts or shared logs.
- Require isolated worktrees for code-writing workers when workspace instructions require them.
- Do not let workers contact the user directly in coordinated multi-agent runs.
- Do not claim completion until verification evidence has been reviewed.
