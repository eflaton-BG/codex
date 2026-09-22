---
name: frontier-image-annotation
description: Prepare, calibrate, and run classification-style image annotation jobs with frontier models using the local bg_ml robot-eligibility tooling. Use for S3 image-list downloads, prompt/config calibration, guarded OpenAI API annotation runs, label evaluation, and handoff for human review; do not use for domain-specific detection or segmentation without an explicit feasibility experiment.
---

# Frontier Image Annotation

Run the internal frontier-model annotation workflow without modifying the user's
`bg_ml` checkout or exposing credentials.

Default repository:
`/home/ezekiel.flaton/devel/bg_ml_ws/bg_ml`

Default annotation and S3 virtual environment:
`/home/ezekiel.flaton/devel/colcon_ws/src/.venv`

Read [references/workflow.md](references/workflow.md) when preparing or executing
a job. Use
[`scripts/frontier_annotation.py`](scripts/frontier_annotation.py) for repository
discovery, dependency checks, job inspection, gated S3 download delegation, and
annotation execution.
Use [`scripts/discover_skus.py`](scripts/discover_skus.py) to obtain an exact
metric-derived unique SKU list.
Use
[`scripts/run_download_s3_images.sh`](scripts/run_download_s3_images.sh) as the
stable command for resumable, size-validated S3 download planning and execution.
It always runs the bundled downloader with the workspace virtualenv.
Use
[`scripts/run_frontier_annotation.sh`](scripts/run_frontier_annotation.sh) as
the stable annotation command. It uses the workspace virtualenv, injects the
OpenAI key from the `openai/transcription` Agent Secrets profile, and defaults
`OPENAI_BASE_URL` to the BG AI Gateway without exposing credentials.
Use
[`scripts/build_review_gallery.py`](scripts/build_review_gallery.py) to create a
deterministic, browser-based sample review with original images, annotated
crops, keyboard controls, autosaved decisions, and CSV export.
Use
[`scripts/run_export_res1_sku_images.sh`](scripts/run_export_res1_sku_images.sh)
for the explicitly approved Pittston RES1 Atlas/log fallback. It injects the
stored MongoDB profile without exposing the password, joins prediction records
to RES1 Elasticsearch save logs, validates S3 objects, and creates durable
mapping artifacts.
Use [`scripts/map_sku_images.py`](scripts/map_sku_images.py) to plan or export one
representative Pittston image per SKU through an explicitly approved
`PickComplete` fallback.

## Operating Rules

- Treat the checked-out repository as user work. Inspect its branch and status;
  never checkout, pull, edit configs, or overwrite files without explicit
  permission.
- Prefer a copied, job-specific YAML config outside the repository for prompt
  iteration. The runner can inject that config without changing `bg_ml`.
- Never ask the user to paste AWS or OpenAI secrets. Use existing environment
  credentials or the `agent-secrets` skill for redacted inspection and
  environment injection.
- Use one virtual environment for the download, calibration, and full run.
  Verify `openai`, `Pillow`, `PyYAML`, and, when downloading, `boto3`.
  The Pittston RES1 Atlas/log fallback additionally requires `pymongo`,
  `dnspython`, and `awscrt` in that environment.
- Activate the workspace environment in an interactive shell with:
  `source /home/ezekiel.flaton/devel/colcon_ws/src/.venv/bin/activate`.
  In automated commands, prefer its absolute Python path.
- Do not install `hvac` into the workspace environment merely for SKU mapping.
  The mapping script delegates Vault-backed Elasticsearch work to the existing
  `bg-elasticsearch` helper, whose `~/bg/myenv` environment already contains
  `hvac`.
- Start with `inspect`. It performs no network calls and prints the selected
  model, image count, output location, dependency state, and repository status.
- Run a diverse, hand-labeled calibration set first. The source procedure
  recommends 50–100 images. Review accuracy and saved crops, then iterate on the
  job-specific config.
- Do not start a billable annotation run until the user explicitly approves the
  reported model, config, image count, and output directory. Pass
  `--approved-billable-run` only after that approval.
- Before enabling `--save-crops`, report free space and projected crop usage.
  Estimate remaining crop space from a representative calibration average when
  available; otherwise conservatively use the remaining source-image bytes.
- A full run may take substantial time and money. Do not infer approval from a
  request to inspect, prepare, calibrate, or create this skill.
- Monitor a running job with the wrapper's read-only `status` command. Report
  completed and remaining labels, percentage, crop count, and latest-label
  timestamp. For a unified-exec job, poll its existing session too; a separate
  `ps` may not see an escalated process.
- After a crash or lost terminal, check durable status before doing anything.
  Do not launch a duplicate while labels are still advancing. If the process
  stopped, resume with the exact approved images, config, model, and output
  location; existing label files are skipped. Reuse the prior billable approval
  only for that exact interrupted job. Any changed input, config, model, count,
  or destination requires fresh approval.
- Annotation concurrency is configurable with `--workers`; the skill stages a
  tuned copy under `/tmp` and never edits `bg_ml`. Start with 4 workers for a
  large run, keep `--api-max-retries 5`, and measure durable progress before
  considering 8 workers. Stop the existing process before changing concurrency;
  never run overlapping annotators against the same output directory.
- Human review remains required. Treat frontier labels as draft annotations
  until reviewed.
- Uploading or publishing labels to S3, Box, FiftyOne, Label Studio, CVAT, or
  another system is a separate external write and requires explicit permission.
- SKU mapping `plan` is read-only. The `export` command writes mapping and
  manifest files and requires explicit approval through `--approved-write`.
- Store reusable SKU lists, mappings, manifests, summaries, and resumable work
  state in one persistent job directory, normally
  `~/Downloads/<job-name>/` or another user-selected root. Keep manifests,
  downloaded images, annotation samples, labels/crops, graphs, review samples,
  and review outputs beneath that single directory. Never scatter a job's
  durable artifacts across `~/Downloads`, rely on `/tmp` for later-session
  artifacts, or place final exports in `/tmp`.
- Moving existing artifacts into a job directory is a write. Inventory the
  proposed paths and obtain explicit approval before creating the directory or
  moving anything.
- Use `SkuRobotEligibilityChange` in `metric_events` as the preferred source for
  the unique SKU list. Report the metric record count and exact deduplicated SKU
  count before mapping.
- Never silently fall back from eligibility-change metrics. If metrics are
  absent, incomplete, or unavailable, stop, identify the proposed alternative,
  and obtain explicit user approval before querying Atlas prediction records,
  Elasticsearch prediction logs, `PickComplete`, or MongoDB.
- Do not download the mapped images automatically after export. Show the mapped,
  unmatched, and ambiguous counts and obtain separate download approval.
- Every download has a hard two-step gate. First run the download planner and
  report total and pending images, GB/GiB, free disk space, projected free space,
  destination, AWS identity, and the generated plan ID. Ask for explicit
  approval. Only then rerun with the exact plan ID and
  `--approved-download`. If the plan ID changes, stop and ask again.
- Always use the user's host AWS credential chain. Never substitute pod,
  container, or cluster credentials. If the host identity check fails, ask the
  user to refresh credentials.

## Typical Commands

Use the stable wrapper rather than activating a shell manually:

```bash
/home/ezekiel.flaton/.codex/skills/frontier-image-annotation/scripts/run_frontier_annotation.sh inspect \
  --images /path/to/calibration/images \
  --config /path/to/job-config.yaml
```

After explicit approval for a calibration API run:

```bash
/home/ezekiel.flaton/.codex/skills/frontier-image-annotation/scripts/run_frontier_annotation.sh annotate \
  --images /path/to/calibration/images \
  --config /path/to/job-config.yaml \
  --workers 4 \
  --api-max-retries 5 \
  --save-crops \
  --approved-billable-run
```

Monitor without making API calls:

```bash
/home/ezekiel.flaton/.codex/skills/frontier-image-annotation/scripts/run_frontier_annotation.sh status \
  --images /path/to/images \
  --config /path/to/job-config.yaml
```

Compare the generated labels with hand labels:

```bash
/home/ezekiel.flaton/.codex/skills/frontier-image-annotation/scripts/run_frontier_annotation.sh evaluate \
  --gold-labels /path/to/calibration/labels \
  --predicted-labels /path/to/generated/labels
```

After the user accepts calibration quality, inspect the full dataset and obtain
fresh approval for its count and destination before running the same `annotate`
command against it.

## Review Samples

After annotation completes, verify that every input has both a label and, when
requested, an annotated crop. Generate the review sample inside the same job
directory:

```bash
/home/ezekiel.flaton/devel/colcon_ws/src/.venv/bin/python \
  /home/ezekiel.flaton/.codex/skills/frontier-image-annotation/scripts/build_review_gallery.py \
  --job-dir /path/to/job \
  --images-dir /path/to/job/images \
  --labels-dir /path/to/job/labels \
  --graph /path/to/job/category-distribution.png \
  --title "Site and Date Annotation Review"
```

The default sample contains up to 25 images from every observed label plus 150
additional globally random images. It writes `annotation-review/index.html` and
`annotation-review/sample-manifest.csv`. The gallery displays the original and
annotated crop side by side, autosaves correct/incorrect/uncertain decisions in
browser local storage, supports keyboard navigation, and exports review results
to CSV. Keep calibration samples and review samples distinct: calibration
samples tune the prompt before the full run; review samples evaluate completed
labels afterward.

## One Image Per SKU Workflow

### Step 1: Export Unique SKUs

Always finish and verify the unique-SKU CSV before looking up image paths.
Prefer `SkuRobotEligibilityChange` metrics and deduplicate non-empty `SkuId`
values:

```bash
/bin/bash /path/to/bg-elasticsearch/scripts/run_bg_vault_elastic_python.sh \
  /path/to/skill/scripts/discover_skus.py export \
  --date-from 2026-08-01 \
  --date-to 2026-08-31 \
  --station-id RES1 \
  --output /persistent/output/unique-skus.csv \
  --approved-write
```

Report the source record count, deduplicated SKU count, output path, CSV row
count including its header, and a duplicate check. When the requested station
cannot be represented by the metric, stop for fallback approval. For the
approved Pittston RES1 fallback, query
`washington_pit_washington_operational.robot_eligibility_prediction_stats`
over the exact window, deduplicate non-empty `sku_id`, and write
`unique-skus.csv`.

### Step 2: Map SKUs to Image File Paths

Do not begin Step 2 until Step 1's CSV exists and is verified. For each
prediction, use the Robot Eligibility Inspector's native join first:

1. read `RobotEligibilityPredictionStats.image_msg_timestamp`;
2. query the site's perception `image_data` collection for
   `timestamp >= image_msg_timestamp` and
   `timestamp < image_msg_timestamp + 1 millisecond`;
3. select the earliest matching record and read `ImageData.file_path`;
4. combine the relative path with the site's configured S3 prefix.

If historical `image_data` records are absent, stop for approval before using
Elasticsearch synchronizer and perception-logger save records as the fallback.
Validate every selected S3 object and reject missing or zero-byte objects.

The Pittston mapper uses `PickComplete`, which is a fallback. Only after
explicit fallback approval:

```bash
/home/ezekiel.flaton/devel/colcon_ws/src/.venv/bin/python \
  /path/to/skill/scripts/map_sku_images.py export \
  --date-from 2026-08-01 \
  --date-to 2026-08-31 \
  --mapping-output /path/to/all-mappings.csv \
  --approved-fallback \
  --approved-write
```

The export creates:

- the complete tote/product/image mapping;
- one selected image row per mapped SKU;
- a headerless S3 URI CSV accepted by `frontier_annotation.py download`;
- an unmatched-SKU CSV.

Prefer a PNG associated only with that SKU. When none exists, use the earliest
valid shared-PNG candidate and flag it as ambiguous.

For Pittston RES1, when the preferred metric query cannot represent the RES1
station and the user has explicitly approved the Atlas/log fallback, export
durable artifacts with:

```bash
/home/ezekiel.flaton/.codex/skills/frontier-image-annotation/scripts/run_export_res1_sku_images.sh \
  --date-from 2026-08-01 \
  --end-utc 2026-08-23T10:10:56Z \
  --output-dir /home/ezekiel.flaton/Downloads/pittston-res1-august-2026/pittston-res1-august-2026-sku-image-manifest \
  --approved-fallback \
  --approved-write
```

For this historical Pittston window, the Atlas `image_data` records are absent,
so the already-approved fallback exporter preserves resumable daily Atlas and
Elasticsearch extracts under `<output-dir>/.work`. It maps prediction image
timestamps to RES1 synchronizer and perception-logger records, validates
non-zero S3 objects, and writes `one-image-per-sku.csv`, `s3-paths.txt`,
`unmatched-skus.csv`, and `summary.json`. Preserve the verified Step 1
`unique-skus.csv`; the final mapping summary must report the same unique-SKU
count.

## Gated S3 Download

Use the fixed skill wrapper for both steps. It does not source shell startup
files or modify credential environment variables; boto3 uses the normal host
credential chain available to the process.

Downloading to persistent local storage is the default, but offer S3 streaming
as an alternative when disk usage is undesirable. The existing BG annotation
script requires local PNG files, so streaming requires a compatible runner that,
for each manifest entry, reads the object with the user's host AWS credential
chain, crops and encodes it in memory, submits the resulting data URL to OpenAI,
and discards the image bytes. Do not pass a private `s3://` URI directly to
OpenAI or silently substitute presigned URLs.

Streaming avoids retaining the dataset locally but does not avoid transferring
every image through the execution host. Before a streaming annotation run,
report the image count, total GB/GiB to be read, AWS identity, selected model,
config, and output directory. Obtain explicit approval for both the S3 reads and
the billable annotation run.

```bash
/home/ezekiel.flaton/.codex/skills/frontier-image-annotation/scripts/run_download_s3_images.sh \
  plan \
  --input-csv /path/to/s3-paths.csv \
  --mapping-csv /path/to/mapping.csv \
  --output-dir /path/to/images
```

Report the complete plan and obtain explicit approval. Then rerun with:

```bash
/home/ezekiel.flaton/.codex/skills/frontier-image-annotation/scripts/run_download_s3_images.sh \
  download \
  --input-csv /path/to/s3-paths.csv \
  --mapping-csv /path/to/mapping.csv \
  --output-dir /path/to/images \
  --plan-id <approved-plan-id> \
  --approved-download
```

For the verified Pittston RES1 August 2026 investigation, read
[references/pittston-res1-august-2026.md](references/pittston-res1-august-2026.md).
