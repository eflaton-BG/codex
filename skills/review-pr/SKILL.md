---
name: review-pr
description: "Review GitHub pull requests for correctness and cross-reference their implementation against linked Jira requirements and Definition of Done, including PRs supplied through Slack threads and companion changes across repositories. Use for PR review or follow-up publication of review findings, not for implementing fixes."
---

# Review PR

Produce evidence-based findings and a concise ticket/Definition of Done (DoD)
assessment. Accept PR URLs, Slack threads containing PRs, optional ticket links,
and a requested review focus.

## Default boundaries

- Review statically and read-only. Reading code, Git metadata, API responses, and
  isolated source snapshots is allowed. Do not execute reviewed code, tests,
  builds, simulations, bootstrap scripts, or container startup unless requested.
- Do not edit application source, switch branches, create worktrees, or turn the
  review into an implementation task. Preserve existing working-tree changes.
- A request to review is not permission to publish. Do not comment, submit a
  review, change PR metadata, or update tickets without explicit authorization
  for that action.
- Follow applicable workspace instructions. Use host-clock timestamps for
  responses when required; do not synthesize timestamps.

## Gather the intended scope

1. Resolve PRs from the supplied links. For Slack input, read the relevant thread
   rather than relying on the linked message or PR title alone.
2. Record each PR URL, repository, base, and exact head SHA. Read its description,
   dependencies, existing review discussion, and reported checks. Distinguish
   actual test results from status-wrapper, formatting, and lint checks.
3. Use the `bga-connections` skill for Slack, Jira, and Confluence context. List
   current connections, select the intended connected entry, and inspect its
   permissions before the first provider call. Use concrete connection UUIDs,
   not refs, for provider operations. Download truncated responses before parsing.
4. Fetch linked ticket descriptions, DoD, status, relevant comments, and
   child/linked-ticket context needed to interpret scope. Do not crawl an entire
   epic unnecessarily or invent acceptance criteria when context is missing.
5. Use host-side GitHub reads, normally `gh pr view`, `gh pr diff`, and REST
   endpoints. Do not require a checkout. If an exact commit is absent locally,
   read it through GitHub or fetch the bounded ref without changing branches.

When mentioning a Jira key, include its summary and a direct clickable ticket
URL on the same line. Surface missing or conflicting requirements explicitly.

## Trace the behavior, not just the diff

- Read changed code and the surrounding control flow, callers, consumers,
  configuration, and tests necessary to establish its behavior.
- Review companion PRs together: check producer/consumer contracts, enablement,
  compatibility, and deployment dependencies.
- Trace the promised behavior from a realistic starting state through success
  and failure. Check relevant invariants such as safety gates, retry budgets,
  state transitions, accounting, cancellation, and fallback behavior.
- Inspect what tests replace with mocks. A test that starts after the difficult
  recovery/state transition does not establish that the transition works.
- Separate a demonstrated defect from a requirement gap, scope ambiguity, or
  missing evidence. Do not present a plausible concern as a confirmed bug.
- Do not recommend weakening safety checks to satisfy a requirement. Do not
  impose physical-robot, simulation, or other validation as mandatory unless the
  requirements or applicable policy demand it.

## Findings and DoD assessment

Lead with actionable findings, highest severity first. For each finding give:

- Priority and a specific title.
- The smallest useful affected file/line range, with a commit-pinned source link.
- Triggering conditions, the failing behavior, and its impact.
- The evidence and requirement involved, including uncertainty where material.
- A suggested correction or decision direction, without implementing it.

Use ordinary review priorities: P0 critical/immediate, P1 high, P2 normal, P3 low.
Assign priority from demonstrated impact, not merely from an unchecked DoD item.
Keep scope questions distinguishable from defects. Report no findings when
appropriate; do not manufacture issues or bury defects under style preferences.

Map each material requirement/DoD item to **met**, **partial**, **unmet**, or
**not verifiable**, with brief evidence. Distinguish implementation from proof.
Clearly separate tests inspected, results reported by the author/CI, and tests
actually executed. Static review must not be described as runtime validation.

Finish with a concise recommendation, reviewed revisions, and limitations.
Related collision/recovery or historical tickets are context, not automatically
additional acceptance criteria for the current PR.

## Moving heads and follow-up requests

Recheck head SHAs before finalizing or publishing. If a head changes, inspect
the delta and affected context, revise findings, and identify which revision
earlier evidence covers. Do not silently attach an older review to a new head.
If updates keep arriving, report the reviewed revision and remaining delta.

For requests to post the findings, read
[Publishing reviews](references/posting.md). Do not publish as part of an
ordinary review.
