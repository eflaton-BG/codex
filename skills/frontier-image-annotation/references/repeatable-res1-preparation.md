# Pittston RES1 annotation preparation

`prepare.py` accepts inclusive Eastern dates, writes beneath one persistent job
directory, and never calls OpenAI or downloads S3 images. It does not edit `bg_ml`.

Use the workspace `.venv/bin/python`. BGA metrics use the current Washington
connection UUID discovered from `list`, after permission inspection. Atlas uses
the stored MongoDB profile injected by Agent Secrets. Before Atlas, perform the
non-authenticating vlogin/connectivity preflight; never invoke vlogin automatically.

## Export the metric-derived inventory

```sh
/home/ezekiel.flaton/devel/colcon_ws/src/.venv/bin/python \
  /home/ezekiel.flaton/.codex/skills/frontier-image-annotation/scripts/prepare.py metrics \
  --date-from 2026-09-01 --date-to 2026-09-30 \
  --job-dir /home/ezekiel.flaton/Downloads/pittston-res1-september-2026 \
  --approved-write
```

Existing inventories are never overwritten. Change dates and job directory for
another run. `SkuRobotEligibilityChange`, `Source=RES`, and `StationId=RES1`
remain the SKU source; mapping never adds Atlas-only SKUs.

## Native Atlas mapping (separate read approval)

```sh
/home/ezekiel.flaton/devel/colcon_ws/src/.venv/bin/python \
  /home/ezekiel.flaton/.codex/skills/agent-secrets/scripts/agent_secrets.py run \
  --profile mongodb/pittston-pickinspector \
  --env MONGO_HOST=public.host --env MONGO_USERNAME=public.username \
  --env MONGO_PASSWORD=private.password --env MONGO_DATABASE=public.database -- \
  /home/ezekiel.flaton/devel/colcon_ws/src/.venv/bin/python \
  /home/ezekiel.flaton/.codex/skills/frontier-image-annotation/scripts/prepare.py native-map \
  --date-from 2026-09-01 --date-to 2026-09-30 \
  --job-dir /home/ezekiel.flaton/Downloads/pittston-res1-september-2026 \
  --approved-native-read --approved-write
```

Confirm the image database and S3 prefix against site configuration before treating
paths as authoritative. Atlas namespace existence is checked. The mapper reads
one prediction and one RGB image window per UTC chunk, not one unindexed scan per
prediction. BSON millisecond timestamps implement the native one-millisecond join.
Only `/pick_scanner/rgb_camera/raw/image` is accepted. No proximity guessing.

Completed daily chunks resume with a signature covering inventory, dates, image
database, topic, and prefix. Matching caches may be reused; incompatible caches
fail closed. Final candidate manifests can be regenerated from these chunks.
Unmatched SKUs and shared-image ambiguity remain explicit. These are native
mapping candidates, **not S3-validated download manifests**.

## Verify the actual S3 objects (HEAD-only, separate read approval)

```sh
/home/ezekiel.flaton/devel/colcon_ws/src/.venv/bin/python \
  /home/ezekiel.flaton/.codex/skills/frontier-image-annotation/scripts/prepare.py validate-s3 \
  --date-from 2026-09-01 --date-to 2026-09-30 \
  --job-dir /home/ezekiel.flaton/Downloads/pittston-res1-september-2026 \
  --approved-s3-read --approved-write
```

Uses only the host credential chain and reports its STS identity. Authentication
and authorization errors stop the job; missing or empty objects are recorded
separately, never included in `one-image-per-sku.csv`. The final mapping includes
exact S3 URIs, non-zero byte sizes, and ETags. `s3-paths.txt` deduplicates shared
objects; it and the validated mapping feed the existing gated download planner.
This does not upload images or download image bodies.
HEAD verification defaults to eight workers (`--s3-workers`, range 1–16), queues
at most 100 objects at a time, and caches each object's host-clock `checked_at`.
An expired host AWS session must be refreshed by the user; do not invoke
`aws login` for them or substitute cluster credentials.

HEAD results are resumable snapshots. For a fresh revalidation, use a new job
directory (or explicitly authorize removal of the specific HEAD cache). Preserve
cached provenance; do not imply historical HEAD results prove current availability.

## Approved synchronizer/save-log fallback

If native mapping has insufficient coverage, obtain explicit fallback approval.
Keep the existing metric inventory; do not invoke the historical all-in-one
exporter's SKU replacement step. Run:

```sh
/home/ezekiel.flaton/devel/colcon_ws/src/.venv/bin/python \
  /home/ezekiel.flaton/.codex/skills/agent-secrets/scripts/agent_secrets.py run \
  --profile mongodb/pittston-pickinspector \
  --env MONGO_HOST=public.host --env MONGO_USERNAME=public.username \
  --env MONGO_PASSWORD=private.password --env MONGO_DATABASE=public.database -- \
  /home/ezekiel.flaton/devel/colcon_ws/src/.venv/bin/python \
  /home/ezekiel.flaton/.codex/skills/frontier-image-annotation/scripts/fallback.py \
  --date-from 2026-09-01 --date-to 2026-09-30 \
  --job-dir /home/ezekiel.flaton/Downloads/pittston-res1-september-2026 \
  --approved-fallback --approved-write --workers 4
```

This uses BGA gateway log reads (connection UUID plus permission inspection),
not Vault/direct ES. Atlas still requires its non-authenticating preflight.
It stages date-bounded prediction records, filters them to metric-derived SKUs,
then uses the historical exporter's approved tote/image timestamp to RGB-save-log
alignment. Default tolerances are 0.1s for synchronizer image timestamps and 3s
for save-log emission times. This is **fallback temporal alignment**, not the
native exact ImageData timestamp join. Keep timing deltas and provenance.
Never infer an S3 filename from ObjectId/time proximity when no save record exists.

Thirty-minute log slices request at most 1,000 hits and split further when
the exact count exceeds that limit or the query times out. A populated
30-minute September slice succeeded after six-hour/10,000-hit requests returned
gateway 502 errors.
Only complete exact hit sets become reusable caches. Completed days and raw
evidence persist beneath `manifest/.fallback-work/`; configuration mismatches
fail closed, including after a partial run. Retry the identical command to
resume. The secret runner may buffer child stdout; monitor completed day JSONs
and each day's `status.json` without launching a duplicate job. A worker failure
writes `last-error.json`, cancels queued days, and asks active workers to stop
between requests. Do not let a failed job silently process the remaining month.

Fallback output is `fallback-one-image-per-sku.csv`,
`fallback-unmatched-skus.csv`, and `fallback-mapping.summary.json`.
After S3 metadata-read approval, run `prepare.py validate-s3` with the same
dates/job plus `--mapping-source fallback --approved-s3-read --approved-write`.
The final verified artifacts are `one-image-per-sku.csv`, `s3-paths.txt`,
`s3-rejected-skus.csv`, and `s3-validation.summary.json`.

To queue metadata verification behind an already-running fallback, use:

```sh
/home/ezekiel.flaton/devel/colcon_ws/src/.venv/bin/python \
  /home/ezekiel.flaton/.codex/skills/frontier-image-annotation/scripts/finalize.py \
  --date-from 2026-09-01 --date-to 2026-09-30 \
  --job-dir /home/ezekiel.flaton/Downloads/pittston-res1-september-2026 \
  --wait-for-fallback --approved-s3-read --approved-write
```

This requires prior S3 metadata-read approval. It takes a per-job lock, waits
up to six hours (configurable), stops on recorded fallback errors, validates
the requested date range, then runs HEAD-only verification. Inspect
`manifest/s3-finalization.status.json` for waiting, verifying, verified, or
stopped state. It never invokes image download or OpenAI commands. If AWS
authentication expires, refresh it interactively and rerun; cached results
are preserved. A queued finalizer is not evidence that verification is complete.

Separate approval is required for any save-log fallback, S3 validation/download,
calibration calls, or full OpenAI run. Zero native coverage is a valid outcome,
not permission to fall back or substitute another sensor/station.

## Local progress and recovery

Read progress without network calls or writes:

```sh
/usr/bin/python3 /home/ezekiel.flaton/.codex/skills/frontier-image-annotation/scripts/status.py \
  --job-dir /home/ezekiel.flaton/Downloads/pittston-res1-september-2026
```

Candidate coverage counts completed days only; active-day log slices can advance
without changing that count. Stored errors are historical artifacts, not a live
process-health check. Confirm the execution session and fresh cache progress.
Downloads explicitly overwrite unvalidated `.pending` responses on retry; complete
validated `.json` caches are preserved. After recovery from a producer error,
the old error file may remain: wait for successful fallback completion before
rerunning the stopped finalizer. Never launch a duplicate producer.

## Validation

```sh
/usr/bin/env PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m unittest discover \
  -s /home/ezekiel.flaton/.codex/skills/frontier-image-annotation/scripts \
  -p 'test_*.py' -v
```
