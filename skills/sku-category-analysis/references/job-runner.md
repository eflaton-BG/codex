# Reusable category-job runner

Use the installed entry point directly:

```text
/home/ezekiel.flaton/.codex/skills/sku-category-analysis/scripts/run_job.sh
```

All orchestration, classification, recovery, service control, monitoring, and
tests execute skill-owned code. Job directories contain only declarative
configuration, input manifests, images/outputs, checkpoints, and provenance;
the runner never imports Python or executes commands from a job or `/tmp`.

This is the default for **new local-image and S3-streaming category jobs**.
Site, dates, input/SKU count, category definitions, account, model, crop,
concurrency, output-token cap, retry budgets and Slack destination are per-job,
not hardcoded in executable scripts. Inventory discovery and S3 mapping remain
the skill's existing preparation steps. Generalization is not permission to run
another model/account or spend.

## Approval boundaries

Ask which account/profile to use for each new billable job, unless explicitly
specified. Record the selected profile and confirmed HTTPS endpoint. Never infer
them from inherited environment variables or the last job.

Recommended reusable approval prefixes are the exact installed script plus one
subcommand. Read and mutation scopes are deliberately separate:

| Mode | Effect |
|---|---|
| `inspect`, `status`, `diagnose`, `aws-lifetime`, `monitor-preview` | Local reads only; status can inspect selected Codex user services |
| `validate` | Local full artifact verification; refuses an active run lock |
| `preflight` | Bounded model listing and/or AWS STS; no inference or S3 image bodies |
| `test` | Offline regression tests with synthetic images, mock APIs and scratch artifacts |
| `prepare` | Explicitly approved new job/config/manifest writes; no API requests |
| `recover` | Explicitly reviewed checkpoint recovery; no model calls |
| `monitor-config` | Local explicit Slack settings; no messages sent |
| `run`, `start` | Explicitly approved inference; separate S3 image-read approval when needed |
| `monitor` | Explicitly approved posts to the recorded existing thread |

Do not wrap commands in ad-hoc Python, `bash -lc`, redirections or pipelines.
Use the absolute entry-point path for approval matching.

## Prepare

Examples below abbreviate that absolute entry point as `run_job.sh`; substitute
the actual paths/profile/account before execution. Configs use the existing BG
YAML contract: `name`, `model`, `task`, `general_rules`, `labels` with unique
names/definitions, and `output.field`. The YAML is copied and hashed.

Local images (recursive PNG/JPEG/WebP; duplicate basenames are supported):

```text
run_job.sh prepare --job-dir JOB --images IMAGES --config CONFIG.yaml
  --account SELECTED_ACCOUNT --credential-source dotenv
  --credential-profile SELECTED_KEY_VARIABLE --credential-file OWNER_ONLY_ENV_FILE
  --base-url CONFIRMED_HTTPS_ENDPOINT --max-output-tokens APPROVED_CAP
  --workers 4 --api-attempts 3 --s3-attempts 3 --approved-write
```

For a keyring field profile, use `--credential-source agent-secrets
--credential-profile SELECTED_PROFILE` and omit `--credential-file`. This uses
the existing Agent Secrets `private.api_key` selector, injected into a child
process; no secret is put in command arguments or saved job artifacts. OAuth
profiles/other selectors are not supported by this runner.

S3 streaming uses the same options, replacing `--images` with:

```text
--manifest VERIFIED_MAPPING.csv --aws-profile AWS_PROFILE
--aws-account EXPECTED_AWS_ACCOUNT --aws-arn EXPECTED_PRINCIPAL_ARN
```

CSV rows need `s3_uri`, `s3_size_bytes` (or `size_bytes`), `s3_etag` (or `etag`).
Optional `SkuId` (or `sku`) values are retained. Repeated image URIs with
identical metadata become one inference input with all associated SKUs.
Conflicting metadata is rejected. No S3 image body is read during preparation.
Local CSVs can instead use `path`, relative to the CSV directory.

`--crop LEFT TOP RIGHT BOTTOM` is explicit; omitting it uses the whole image.
No Pittston camera crop is assumed. Positive crop bounds are checked against
the actual image before inference. Output filenames are stable input indices.

Preparation refuses a nonempty job directory. It returns the immutable
`plan_id`, count, bytes, selected account, and config model. Inspect/report the
plan before asking for S3/inference approval.

## Verify and execute

```text
run_job.sh inspect --job-dir JOB
run_job.sh preflight --job-dir JOB --account SELECTED_ACCOUNT
run_job.sh run --job-dir JOB --account SELECTED_ACCOUNT --plan-id PLAN_ID
  --approved-billable-run --approved-s3-reads
```

Omit `--approved-s3-reads` for local-only jobs. To survive the shell/app session,
use `start` instead of `run` with the same arguments. Deterministic per-job
systemd user units invoke only this installed entry point. Service restart is
disabled: restarting a process is not permission to replay uncertain requests.
`launched` is not proof of progress; verify fresh state, advancing completed
counts and successful checkpoint response models.

The job identity pins account/profile/endpoint, YAML, full normalized inputs,
local image hashes, S3 size/ETag, crop/detail, caps/policy and implementation
hashes. Changing one blocks resume. A runner upgrade requires an explicit
reviewed/versioned migration; it does not authorize reclassification.

## Resilience and recovery

- SDK retries are zero. Application retry budgets are explicit total attempts,
  default three each for model API and whole-object S3 reads, independently.
  Backoff is bounded (1s, 3s, 9s, 27s according to configured total attempts).
- API retries cover connection/timeouts, HTTP 408/429/5xx. Authentication,
  invalid-request, incomplete/wrong-model, and invalid-category results stop;
  they are not repeatedly retried with unchanged settings.
- S3 retries close failed bodies, re-read the whole object and preserve
  `IfMatch` ETag. Truncated bodies are retryable transport failures.
- AWS uses the explicitly selected profile and a fresh per-thread session.
  On expiry, gate new model requests, try one fresh native STS check, then wait
  for changed local credential-cache metadata before another check. The same
  expected account and principal must pass. Never runs `aws login` or `vlogin`.
  Two auth recoveries/read, at most 10 changed-cache probes or 24h/pause.
- Access-credential expiry is refreshable and is **not** the hard login-session
  expiry. The latter is unknown unless explicitly exposed by the cache.
- Each model/S3 attempt is journaled before invocation. Saved responses precede
  semantic parsing. Successful checkpoints and all three output hashes are
  validated and skipped. Failed/uncertain attempts block a new run.

Inspect failures, then explicitly recover selected records:

```text
run_job.sh diagnose --job-dir JOB
run_job.sh recover --job-dir JOB --account SELECTED_ACCOUNT --plan-id PLAN_ID
  --index IMAGE_INDEX --reason REVIEWED_REASON --approved-write
```

This archives the original checkpoint and prior S3 attempt journals, validates
all preserved successful outputs, and permits only work with **no model request
or API journal**. Multiple `--index` arguments are fully checked before mutation.
It does not start inference. Then execute the same pinned plan with approval.

`--finish-response` instead permits local output completion from an already
saved, completed, same-model, valid-category response with an unchanged crop.
It does not call the API or reset its journal budget. An uncertain API attempt
without such a response is not automatically recoverable.

## Optional Slack updates

No channel, thread, connection UUID, Slack credential or inference account is
assumed. Configure an existing destination and a separately selected BGA
credential locally:

```text
run_job.sh monitor-config --job-dir JOB --label RUN_LABEL
  --channel-id CHANNEL_ID --thread-ts EXISTING_THREAD_TIMESTAMP
  --credential-file OWNER_ONLY_ENV_FILE --credential-profile SELECTED_BGA_KEY
  --gateway-url CONFIRMED_BGA_GATEWAY_URL --approved-write
run_job.sh monitor-preview --job-dir JOB
```

Then add `--approved-thread-updates` to `start`, or explicitly run:

```text
run_job.sh monitor --job-dir JOB --approved-thread-updates
```

Monitoring rediscovers the current connected Slack UUID and checks enabled
`chat.postMessage` permission before posting. Messages identify
`ZekesCodexFriend`, use only the existing thread, and never broadcast. Defaults
to five minutes plus status transitions; reports concrete failed image/stage/
cause/action, worker exit/stale-heartbeat diagnostics and AWS expiry semantics.
Ambiguous sends are journaled and not immediately replayed. Receipts verify the
channel/thread and are saved under `.job/slack/`.

## Artifacts and completion

- `.job/identity.json`, `plan.json`, `state.json`, `run.lock`
- `.job/records/image-NNNNN.json`, per-attempt journals and recovery archives
- `product_annotation.yaml`, `manifest/inputs.json`
- `crops/`, `annotated/`, `labels/`
- `classification-results.csv` after all outputs validate (one row per SKU,
  or one per image when SKU is absent)

Predictions remain draft labels for human visual review. Successful API
response usage does not prove that failed attempts cost nothing.

## Existing jobs

Do **not** adopt or migrate an already running/historical job into this schema
just to use the new CLI. Keep its exact pinned runner and outputs. The legacy
October scripts are retained for provenance/compatibility, not the default for
future runs. `status` and `diagnose` can read older jobs; legacy mutation and
preflight retain their existing specific entry points.
