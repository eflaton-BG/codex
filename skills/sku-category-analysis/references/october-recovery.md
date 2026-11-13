# Pinned October Stride/Terra recovery

Use the installed `scripts/run_october_terra.sh` directly. All new executable
code and tests live inside this skill; services invoke that exact installed
entry point, never `/tmp` scripts. Historical, hash-pinned job runtime and
provenance remain in the existing Downloads job.

Read-only commands:

```text
run_october_terra.sh status
run_october_terra.sh diagnose
run_october_terra.sh preflight
run_october_terra.sh aws-lifetime
run_october_terra.sh audit --account Stride
run_october_terra.sh test
```

The following command is mutating/billable and requires explicit user
authorization plus runtime approval. It archives only image 21718's reviewed,
pre-model ExpiredToken failure, preserves 21717 successes and all provenance,
and launches the pinned job and existing-thread Slack watcher:

```text
run_october_terra.sh restart --account Stride --approved-restart --approved-thread-updates
```

Do not use it for a different failed image or another run. It refuses to reset
an existing recovery or replay a failed/uncertain model attempt. The guard still
validates model, account, config, manifests, identity and successful artifacts.

Saved approval prefixes can be scoped separately to `status`, `diagnose`,
`preflight`, `aws-lifetime`, `audit`, `test`, and the explicitly authorized
`restart` operation. Read approval never implies restart/inference approval.

Policy: four workers; gpt-6.1-terra; Stride; reviewed 11 labels; 2048 output-token
cap. SDK retries zero. Three total transient API attempts and three total
transient S3 attempts, with 1s/3s backoff, retain separate journals.

Each worker uses a fresh-session S3 client. On AWS expiry, gate new model
requests and try one fresh native-session STS identity check. If unsuccessful,
pause for a locally changed AWS cache and recheck the pinned identity, without
running login. At most 10 changed-cache checks or 24 hours per pause, and two
auth recoveries per S3 attempt. No inference replay is part of auth recovery.
Already in-flight model calls may finish.

Slack posts every five minutes and on pause/resume/terminal transitions, only to
the existing private thread, with concrete checkpoint reasons/actions. Messages
distinguish refreshable access-credential expiry from hard login-session expiry,
which may be unknown. Stop reasons that have no checkpoint explicitly require
service/log inspection rather than an invented diagnosis.
