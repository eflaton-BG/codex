---
name: people-review-writer
description: Generate evidence-based manager reviews, promotion justifications, performance summaries, and feedback drafts for a person using local notes, Box documents, Jira, and GitHub. Use when Codex needs to gather multi-source evidence about an employee, summarize strengths and growth areas, or produce review-ready text with concrete examples and links.
---

# People Review Writer

## Overview

Use this skill to build a review from evidence instead of memory. Prefer recent, dated artifacts. Distinguish facts from inference. If a source cannot be read, say so explicitly and do not pretend it was covered.

## Workflow

### 1. Define scope first

Establish:

- person name
- review type: manager review, promotion justification, calibration notes, or summary bullets
- time window if the user gives one

If the user does not specify a window, use a recent window that matches the request and state the exact dates used.

### 2. Gather local notes

Search the user's home directory for the person's name and common variants.

Prioritize:

- Obsidian vault notes
- 1:1 notes
- standup or sync notes
- prior review notes

Ignore noisy matches like Slack webapp logs unless they contain actual message content.

Pull only the notes that add evidence about:

- ownership
- delivery
- collaboration
- review feedback
- goals
- growth areas

### 3. Gather Box evidence

Always check the people-review Box folder first:

- `https://berkshiregrey.app.box.com/folder/224282267914`
- use folder ID `224282267914`
- match in this order:
  exact full name
  work email
  cautious close variants only if the exact match is absent
- do not assume a near name match is correct when multiple people could plausibly match
- identify the matching person-specific file or subfolder and extract the associated notes before moving on to broader Box searches

If the user gives a Box folder or file, or after checking the default people-review folder:

- prefer the local toolkit at `~/devel/mcp-server-box` when available
- use the folder ID directly when it is visible in the URL
- list the folder contents first so you can identify which files matter
- extract text from the relevant docs instead of relying on titles alone

If a Box extract returns empty content, say that clearly in the final answer and do not treat the file as reviewed evidence.

### 4. Gather Jira evidence

Use Atlassian MCP tools directly.

Recommended sequence:

1. resolve `cloudId` with `getAccessibleAtlassianResources`
2. resolve the person's Jira account with `lookupJiraAccountId`
3. query assigned work with JQL, usually split into:
   - active work
   - done work
4. fetch a few representative issues in detail when they are likely to be cited

Prefer recent issues that show:

- meaningful ownership
- technically difficult work
- operational impact
- cross-team collaboration

When citing Jira tickets in a response, include the summary and direct URL on the same line.

### 5. Gather GitHub evidence

Do not rely on org-wide GitHub search alone for private repos. It can underreport or fail even when the user and token are valid.

Preferred GitHub sequence:

1. verify the GitHub login if needed with `gh api users/<login>`
2. use repo-scoped `gh pr list` queries for authored, reviewed, and commented PRs
3. use [scripts/collect_github_prs.py](scripts/collect_github_prs.py) when possible
4. inspect representative PRs with `gh pr view` for body, review state, and changed-file counts

Treat authored PRs as delivery evidence and reviewed/commented PRs as collaboration evidence. Exclude self-authored PRs from the review/comment sections when summarizing review participation.

### 6. Synthesize, do not dump

Turn the evidence into themes:

- strongest contributions
- impact on team or product
- collaboration patterns
- growth areas
- promotion readiness, if requested

Prefer a few concrete examples over a long inventory.

Good growth areas are:

- supported by evidence
- specific
- coachable
- framed as next-level expectations rather than vague criticism

### 7. Produce a review-ready output

Default output shape:

1. short assessment paragraph
2. strengths paragraph
3. growth area paragraph
4. paste-ready manager review paragraph
5. compact source note with any important caveats

When the user wants prose they can paste into a review system, the main response should sound like a professional manager summary rather than an evidence dump. Prefer natural review language, concise examples, and short descriptions of work instead of raw ticket-number-heavy writing in the main prose.

Also include a separate reference section for the user's validation. Keep this distinct from the main prose. In that section, list the supporting Jira tickets, PRs, Box files, or local notes that back the statements so the user can spot-check the claims quickly.

When the review drafting work is complete, offer a concise wrap-up summary of what was produced and what evidence sources were used. If helpful, summarize the final stance of each major review theme so the user can quickly resume or hand off the work later.

When the user is iterating on a review across a long session, or when the session is likely to be resumed later, save a reusable handoff note under `/tmp` that captures:
- the review subject
- evidence sources used
- major conclusions by section
- important user wording preferences
- any near-final text that should be preserved

The goal is to make it easy to resume in a later session without reconstructing the evidence or the writing decisions.

When useful, add a short evidence list after the draft. Keep the main review prose natural and ready to paste into a review system.

## Quality Bar

Use exact dates when describing recency.

Call out uncertainty when:

- a Box doc could not be extracted
- GitHub evidence appears incomplete
- the review window is inferred
- a judgment is based on a small sample

Avoid:

- overstating promotion readiness from a thin record
- copying long ticket or PR bodies
- treating assigned tickets as proof of delivered impact
- using generic praise without examples

## Script

Use [scripts/collect_github_prs.py](scripts/collect_github_prs.py) to collect repo-scoped GitHub authored, reviewed, and commented PRs. It is useful when org-wide GitHub search misses private repo activity.
