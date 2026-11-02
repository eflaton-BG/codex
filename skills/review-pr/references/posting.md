# Publishing reviews

Read this only when the user requests publication of review findings. It does
not grant permission to publish or change account permissions.

## Authorization and identity

- Confirm the requested action and destination are covered by explicit user
  authorization. "Review this PR" is read-only; "post the findings on this PR"
  authorizes a comment review, not an approval, merge, or formal
  `REQUEST_CHANGES` review.
- For companion PRs, publish findings relevant to each authorized destination
  and cross-link the fuller assessment rather than creating redundant threads.
- Default to **BG Agents** when publication is requested without an identity.
  An explicit **"as me"** request selects the user's personal identity.
- These are separate routes. Enabling BG Agents posting does not replace or
  disable personal GitHub access. Never silently fall back between identities.
  If the selected route is unavailable, explain the blocker and stop.
- A formal `APPROVE` or `REQUEST_CHANGES` submission needs explicit authorization
  for that disposition and a route that supports the requested identity/event.
  The BG Agents helper below supports only `COMMENT`; do not substitute a
  different account or event to bypass that limitation.

## Pre-publication checks

1. Read current heads and existing reviews/comments. Check whether this finding
   set was already posted, including when retrying after a timeout.
2. Ensure the body reflects the reviewed revisions. If a head moved, review the
   delta before posting; do not merely replace the expected SHA.
3. Keep only evidence-supported findings and relevant DoD context. Include
   commit-pinned links. Every Jira key must appear with its summary and a
   direct clickable ticket URL on the same line.
4. Keep formal review disposition distinct from prose recommendations.
5. Submit sequentially and retain each returned review ID and URL.

## BG Agents route

Use the installed `bga-connections` skill and its existing
`post-github-review` command; do not recreate the authenticated transport,
extract tokens, or use the personal connection's normal review endpoint as a
bot substitute.

Before the first provider operation, list connections and inspect permissions
for the current connected GitHub UUID. The platform helper derives the principal
and connection, checks repository access and the expected head, and posts a
`COMMENT` review:

```text
post-github-review --owner OWNER --repo REPO --pull-number NUMBER \
  --head-sha REVIEWED_HEAD --body REVIEW_TEXT --explicit-user-request
```

Resolve the installed CLI and Python executable and invoke their absolute paths
on the host. Pass Markdown safely as a literal argument; do not allow shell
expansion of backticks or other review-body content.

If the response is `local_platform_action_disabled`, ask the user to enable
**AI Gateway → Local Connection Access → Post GitHub reviews as BG Agents**.
Normal GitHub Connection review permission is separate from this platform
action. Do not change the setting or post under the user's identity instead.

## Personal route ("as me")

Use a verified personal GitHub route, normally the host's authenticated `gh`.
Read `gh api user` first and identify the actual account; do not assume a login
from a previous session. Stop if it is not the intended user or is ambiguous.
Do not sign out, switch credentials, or borrow another account to proceed.

Create the authorized review using the current reviewed commit. For a comment
review, the GitHub REST operation is:

```text
POST /repos/OWNER/REPO/pulls/NUMBER/reviews
{"commit_id": "REVIEWED_HEAD", "event": "COMMENT", "body": "REVIEW_TEXT"}
```

Use a properly serialized JSON body file rather than interpolating Markdown
into shell expressions. A connected personal GitHub route is also valid if
its acting identity has been verified and its permissions inspected.

## Verification and retry boundaries

Read the saved review through GitHub and verify author/login and account type,
state, commit, body, and URL. For BG Agents, verify the returned account is the
expected bot, not the connected personal user. If verification is incomplete,
report that precisely; do not claim the identity was verified.

On a definite pre-dispatch rejection, fix only the authorized precondition and
retry when released. On a timeout or unknown outcome, inspect existing reviews
before retrying; never blindly submit the same review again. If the outcome
remains ambiguous, stop and report it. Retry successful destinations only if a
new update was explicitly requested, not because a companion submission failed.

Return direct links to every posted review and identify the verified author.
Disclose any automatic on-behalf-of attribution; bot authorship does not imply
anonymous publication. Do not claim a formal approval or request-changes event
when the saved state is `COMMENTED`.
