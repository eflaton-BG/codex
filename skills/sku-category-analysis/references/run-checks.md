# Rule-friendly run checks

Use the installed entry point directly:

```text
/home/ezekiel.flaton/.codex/skills/sku-category-analysis/scripts/run_checks.sh
```

It supports only inspection. It cannot annotate, read S3 image bodies, post to
Slack, restart services, run login, install dependencies, or import job code.
Do not wrap it in Python, `bash -lc`, pipelines, or redirections; direct
invocations preserve command-prefix approvals.

## Suggested approval rules

Approve the exact script path plus a subcommand to keep scopes separate:

- `.../run_checks.sh status`: local state, optional read-only `systemctl --user show`.
- `.../run_checks.sh diagnose`: bounded local logs and failed checkpoints.
- `.../run_checks.sh aws-lifetime`: local AWS login-cache expiry metadata only.
- `.../run_checks.sh preflight`: bounded STS identity/model-list requests, no
  inference. AWS's native credential provider may refresh its existing cache.

Approving the script path alone covers all four read-only modes, including the
non-billable network preflight. Do not approve arbitrary Python or a runner's
`run`/`prepare` subcommands just to allow diagnostics. These approval rules do
not authorize starting or modifying a run.

Examples (replace `JOB` with the actual run output directory):

```text
run_checks.sh status --job-dir JOB --service codex-pittston-terra-october-2026-resume-2.service
run_checks.sh diagnose --job-dir JOB
run_checks.sh aws-lifetime
run_checks.sh preflight --job-dir JOB --account Stride
run_checks.sh preflight --job-dir JOB --account Stride --scope aws
```

Use the full absolute script path shown above when executing these examples.

## Expiry semantics

Report `access_credentials_expire_at` and its local-time rendering. AWS login
access credentials are refreshable; their near-term expiry is **not** the login
session's hard expiry. Report the login-session expiry as unknown unless the
cache explicitly supplies it. Refresh material being present does not prove
that the server will accept it.

Never print token/cache contents, access keys, dotenv values, request headers,
or image data. Preflight uses the recorded account/model, restricts the
credential file and gateway endpoint, creates a fresh AWS SDK session, and
verifies the AWS account/principal against the pinned job identity.
