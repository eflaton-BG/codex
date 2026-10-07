# Guarded annotation execution

## Select the account, never infer it

Before **each new billable run**, ask which account/profile the user wants.
If they already name it in the request, confirm that choice rather than asking
them to repeat it. Do not choose based on shell state, a previous run, or a
convenient key. An exact interrupted-job resume keeps its recorded selection;
never switch accounts during recovery. No default profile or endpoint exists.

The wrapper's `inspect`, `status`, `validate`, and `evaluate` are local and do
not need credentials. `annotate` requires explicit source/profile/endpoint plus
`--approved-billable-run`. A selected profile is not permission to spend.
An account-attribution verification should use one approved next image in an
isolated sample job; ask the user to confirm attribution before the main run.

Supported selections:

- `--credential-source agent-secrets --credential-profile <selected-profile>`
  reads that keyring profile's `private.api_key` via Agent Secrets injection.
- `--credential-source dotenv --credential-profile <selected-variable-name>
  --credential-file <owner-only-file>` parses exactly one literal assignment.
  Never evaluate shell syntax or print the value.
- Both require `--base-url <confirmed-https-endpoint>`. Inherited API key,
  base URL, organization and project values are removed before injection.

Stride is one possible choice, **not a default**. Its existing local setup may
use `BG_AI_GATEWAY_STRIDE_KEY` in `~/.codex/.env` and the endpoint configured in
`~/.codex/stride.config.toml`. Discover/verify configuration without dumping
secrets. Other accounts can use different profiles; do not substitute one.

## Inspect, reuse, and launch

1. Inspect the intended inputs and job-specific config using the standard wrapper.
   Report count, config/model, output, selected account, workers, retry limit,
   free space, projected crop growth, and approval scope.
2. Calibration outputs can be imported with repeatable `--reuse-from <sample-job>`.
   Each source has `product_annotation.yaml`, `images/`, and
   `images_labels_<model>/`. Import verifies identical config hashes and source
   image hashes, enum labels, and readable crops; differing outputs are never
   overwritten. The source model/config must match exactly.
3. Run `annotate` with the explicit credential arguments from above. Crops are
   always saved. Default concurrency is 4; SDK retries default to 5 and may
   add billable requests. There is no second application-level retry loop.
4. Output is `<images>_labels_<model>`. Its `.job/` holds the account identity
   (no secret), config snapshot, input metadata signature, lock, state,
   per-attempt JSONL result journals, reuse provenance, and final validation.
   Successful responses record available response IDs and token usage.

The identity check prevents an accidental resume with a changed config, source,
input inventory, endpoint, or account. The input signature uses relative paths,
sizes and modification times; it is not a full content hash. Calibration imports
do use content hashes. Preserve inputs while a job is active.

Existing untracked outputs are not silently adopted. For older jobs, retain
the older exact resume path, or explicitly prepare a separate job and import
verified compatible results. Never rerun completed images merely to migrate
runner metadata.

## Failures, monitoring, completion

The lock spans the full API-processing run. A label is written atomically after
its crop, and completed labels are skipped. Failures are journaled immediately;
the diagnostic path never reads the SDK's potentially failing `output_text`
property again. Queued work is bounded by worker count. By default, 20
consecutive failures stop new scheduling; in-flight calls finish, and the job
exits incomplete. This is not permission to retry indefinitely.

`--progress-seconds` controls timestamped process heartbeats (default 60), not
automatic chat messages. Use durable `status` plus the original process session
for user-requested chat updates. A stale log does not prove workers stopped.
Confirm process exit before resuming the exact authorized job; inspect failures
and do not retry unchanged persistent errors repeatedly.

`validate --images ... --config ...` requires matching input/label/crop sets,
valid category values, and nonzero crop sizes. It makes no API calls and does
not establish image readability or annotation accuracy; verify sampled PNGs
for review and retain human review as a separate quality gate.

## Review package and graph

```bash
/home/ezekiel.flaton/devel/colcon_ws/src/.venv/bin/python \
  /home/ezekiel.flaton/.codex/skills/frontier-image-annotation/scripts/build_review_package.py \
  --job-dir /path/to/job --labels-dir /path/to/job/images_labels_MODEL \
  --month 'Month YYYY' --sample-size 300 --seed 202609
```

The seed is explicit and reusable; choose any recorded integer. The generator
maximizes equal per-category quotas, includes all examples of scarce classes,
then fills the remainder randomly without replacement. It refuses to overwrite
an existing gallery or graph. The gallery links existing originals and crops,
so keep the job directory together. Validate sample uniqueness, asset URLs,
PNG readability, and JavaScript syntax before handoff.

The full-population graph joins predictions to unique SKU IDs in
`manifest/one-image-per-sku.csv`; it is not the balanced review distribution.
Shared images count once per associated SKU on the SKU chart, but only once
for annotation billing/review. Report both denominators. Preserve S3 mappings:
downloads/reviews do not upload or relocate source S3 objects.

Review dates are UTC image-folder dates. Inventory selection uses its explicit
site-local date window; an adjacent UTC date is not automatically out of scope.
Post-review analysis and category-policy changes are intentionally separate.
