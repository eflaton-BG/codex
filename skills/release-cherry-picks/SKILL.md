---
name: release-cherry-picks
description: Reconcile a release cherry-pick sheet, page, or patch list against target branches; gate local picks and pushes separately, record pick SHAs in source order, restore previous branches, and remind the user about release-candidate tagging.
---

# Release Cherry-Picks

Use for release backports driven by a user-selected workbook tab, page, patch
list, or similar source of truth. This workflow has two mandatory user
checkpoints. Reading a list does not authorize picking, and approving local
picks does not authorize pushing.

## Discover and reconcile — no code or branch changes yet

1. Read the exact source page/tab/patch the user selected. Use the available
   spreadsheet, Box, or BGA Connections skill when needed. Do not substitute a
   similarly named sheet or infer a release branch solely from its version.
2. Capture each row in original order: ordinal, source locator (sheet and row,
   page item, etc.), repository, PR URL, requested source commit, summary, and
   existing recorded pick commit. Exclude examples and headers. Preserve
   repeated rows as references, but do not apply the same patch twice.
3. Establish the exact target branch and remote for each repository. If they
   cannot be established from the request and evidence, ask. Record each
   repository's original branch, or original HEAD if detached, before checkout.
4. Inspect cleanliness, in-progress operations, worktree ownership, the current
   remote target tip, and source commit identity. Resolve abbreviated hashes
   against the correct repository and verify they match the listed PR.
   Read remote state without changing branches; fetch needed objects when safe.
5. Classify every row against the target, not the current development branch:
   **already picked**, **needs pick**, or **blocked/uncertain**. Check ancestry,
   provenance trailers, and patch equivalence; do not rely on matching titles or
   a spreadsheet approval flag. For a single source commit, a bounded check is:
   `git cherry <target> <source> <source>^`.
   An empty result can mean ancestry; `-` means patch equivalence and `+` means
   no equivalent patch found. Identify the actual target-side SHA for already
   picked rows. Check reversions or partial applications when evidence conflicts.
6. Resolve the allowed branch workflow under applicable workspace rules.
   Explicitly obtain any required exception for direct release-branch commits;
   otherwise propose exact review branch names and obtain approval. Do not
   silently create review branches or bypass protected-branch rules.

## Checkpoint 1 — stop for approval

Present:
- Exact source selection, target branch and remote per repository, and inspected
  target baseline SHAs.
- An original-source-order table identifying already-picked rows with their
  existing target SHA, pending rows with their source SHA, and blocked rows
  with a reason.
- The proposed pick order and any dependencies or required branch exception.

Ask for approval to perform the pending local picks, then **stop**. A general
request to process a new sheet does not waive this checkpoint. Do not proceed
with uncertain rows by guessing.

## Apply approved local picks and keep a resumable ledger

- Recheck the approved source, target tips, cleanliness, and duplicate status.
  If the approved set or baseline changes materially, return to checkpoint 1.
- Check out the approved targets. Fast-forward existing local release branches
  only; never reset user commits or rebase. Creating a local tracking branch
  for an explicitly approved target is not permission for unrelated branches.
- Pick one source commit at a time using `git cherry-pick -x`, preserving source
  order within each repository and approved dependency order. Inspect parent
  count; do not assume a PR merge SHA needs `-m`. For a real merge commit,
  establish and approve the intended mainline before applying it.
- Immediately record each full resulting SHA and provenance before the next
  pick. Keep a machine-readable ledger under `/tmp/codex-session-notes/` plus a
  concise update to `latest.md`, using a lock for shared notes and host-clock
  timestamps. Include the original branch/HEAD, baseline, source ordinal and
  locator, repository, PR URL, full source SHA, full pick SHA, status, and tests.
  Retain existing target SHAs for rows already applied.
- Stop on conflicts, unexpected empty picks, divergence, or missing dependencies.
  Record completed work and the blocker. Do not invent source fixes, silently
  skip rows, abort/reset completed work, or weaken tests.
- Run affected existing/source-added regression tests and configuration checks.
  Follow workspace container/build instructions; report real environment or
  build blockers separately from test failures. No unrelated formatting.

## Checkpoint 2 — stop before any push

Present the exact target branch for each repository, completion/conflict status,
and actual validation results. Provide **all pick commits in the order the rows
appear in the spreadsheet/source**, even when execution was grouped by repo.
Include row/cell locator, repository, source SHA, resulting or existing pick
SHA, and whether it was newly applied or already present. Point to the saved
full-SHA ledger.

State clearly that nothing has been pushed. Leave the target branches checked
out for inspection and **wait for explicit approval to push these commits**.
Do not treat an unrelated “go ahead,” pick approval, or request to create this
skill as push approval. If picks are incomplete or validation is blocked, say so.

## Push only after the checkpoint-2 go-ahead

1. Recheck the exact approved commits, working trees, branch tips, and current
   remote state. If remote changes require new integration, stop and request
   direction; do not rebase or silently expand the approved commit set.
2. Push only the approved branch(es) to their intended remote(s), using explicit
   refspecs and ordinary non-force pushes. Never push tags or unrelated branches,
   create a PR, or bypass branch protection. Record partial successes if a later
   push fails; do not announce overall success or restore branches prematurely.
3. Verify every approved remote branch points to the expected tip. Record the
   published SHAs and push results in the ledger.
4. After all pushes succeed, restore each repository's original branch or
   detached HEAD. If the original state was not captured, offer `2204_devel` as
   the default rather than guessing. Do not discard intervening user work.
5. Remind the user that they may want to run the Jenkins branch-cut/tag job to
   create a new release candidate:
   https://jenkins2.bos.berkshiregrey.com/job/Release/job/bg_branch_cut_and_tag/
   Do not run the job automatically.

Spreadsheet/page writeback is a separate action requiring explicit user
approval. Preserve the source file until that approval; the ordered ledger is
the handoff for a later writeback.
