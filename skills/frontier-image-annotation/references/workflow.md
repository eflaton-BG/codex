# Frontier Annotation Workflow

Source procedure:
`https://berkshiregrey.atlassian.net/wiki/spaces/RPS/pages/3475865607/How+To+Annotations+with+a+Frontier+Model`

The source page was read at version 7, dated 2026-08-22. Re-read it through the
connected Confluence provider when the user asks to synchronize the skill with
the latest procedure or when the local repository no longer matches this guide.

## Fit Check

Frontier annotation is most suitable for tasks such as image classification.
Treat domain-specific object detection and segmentation as experimental: first
test the current candidate model on a representative calibration set.

## Current Local Layout

The expected checkout is:

```text
/home/ezekiel.flaton/devel/bg_ml_ws/bg_ml
```

On the inspected branch, the relevant files are:

```text
experiments/robot_eligibility_classifier/
├── configs/product_annotation.yaml
├── configs/product_annotation_multiclass.yaml
├── scripts/annotation/openai_product_annotation.py
└── scripts/data_helpers/download_s3_images.py
```

The Confluence page shows older script paths. Discover files rather than assuming
those paths.

The inspected annotation script also resolves its config beneath
`scripts/annotation/configs/`, while the repository configs live in
`experiments/robot_eligibility_classifier/configs/`. The bundled runner stages
the selected script and config outside the repository so the job can run without
editing or symlinking the checkout.

## Virtual Environments

Use this environment for frontier annotation, image processing, and S3
downloads:

```bash
source /home/ezekiel.flaton/devel/colcon_ws/src/.venv/bin/activate
```

For non-interactive execution, prefer:

```text
/home/ezekiel.flaton/devel/colcon_ws/src/.venv/bin/python
```

It contains `openai`, `boto3`, Pillow, PyYAML, and requests.

The Pittston RES1 Atlas/log fallback also uses `pymongo`, `dnspython`, and
`awscrt`. Keep those dependencies in the same workspace environment so the
fixed export and download wrappers use one credential and dependency context.

Run annotation commands through:

```text
/home/ezekiel.flaton/.codex/skills/frontier-image-annotation/scripts/run_frontier_annotation.sh
```

The wrapper preserves the virtualenv Python path, injects `OPENAI_API_KEY` from
the `openai/transcription` Agent Secrets profile, and defaults
`OPENAI_BASE_URL` to
`https://agents-gateway.berkshiregrey.com/ai-gateway/codex/v1`. Do not source
shell startup files, print the secret, or put it on the command line.

Vault-backed Elasticsearch access uses `bg_vault_elastic`, whose client imports
the `hvac` package. The workspace environment does not contain `hvac`, but
`/home/ezekiel.flaton/bg/myenv` does. The SKU mapper therefore delegates
Elasticsearch operations to the existing `bg-elasticsearch` helper instead of
requiring another package installation.

## One Image Per SKU

Use `scripts/discover_skus.py` first with an inclusive local date range.

1. Query `SkuRobotEligibilityChange` from `metric_events` for the requested
   customer, site, station, and local date range.
2. Report the total matching metric records and exact number of distinct,
   non-empty `SkuId` values.
3. Before writing the sorted unique list and provenance summary, obtain approval
   and use `discover_skus.py export --approved-write`.
4. If metrics are absent, incomplete, or unavailable, stop. Name the proposed
   fallback and obtain explicit approval before querying it. Prediction
   statistics, prediction logs, `PickComplete`, and MongoDB are all fallbacks.
5. `map_sku_images.py` uses `PickComplete`; it requires
   `--approved-fallback` for both planning and export.
6. For each mapped SKU, prefer an exact non-zero S3 image path. Preserve every
   unmatched SKU and the mapping provenance.
7. Report counts and run the mandatory download plan before asking separately
   for download approval.

### Durable Artifacts

- Select one persistent job root such as `~/Downloads/<job>/` or a
  user-selected alternative before writing durable artifacts. Keep all
  manifests, query state, downloaded images, calibration samples, labels,
  crops, graphs, review samples, and review results beneath it. A useful layout
  is:

  ```text
  <job>/
  ├── manifest/
  ├── images/
  ├── samples/calibration/
  ├── labels/
  ├── graphs/
  └── annotation-review/
  ```

- Do not scatter one job's artifacts across the top level of `~/Downloads`.
  Before consolidating existing paths, inventory them and obtain explicit
  approval because moving files is a write.
- Do not use `/tmp` for artifacts that a later session or download will need.
- Pittston RES1's approved Atlas/log fallback uses
  `scripts/run_export_res1_sku_images.sh`. It reads the
  `mongodb/pittston-pickinspector` keyring profile through `agent-secrets`,
  uses the workspace virtualenv, and rejects `/tmp` output.
- Keep `<output-dir>/.work`; its complete per-day CSVs and S3 HEAD cache allow
  reruns to resume without repeating completed slices.
- Elasticsearch synchronizer and perception-save extraction is additionally
  split into durable six-hour files by default so a slow full-day scroll can
  resume without repeating completed sub-day windows.
- The fallback exporter requires both `--approved-fallback` and
  `--approved-write`.

The date range is interpreted in `America/New_York`. The end date is inclusive.

## S3 Download Gate

Use `scripts/run_download_s3_images.sh`. It invokes the bundled downloader with
the fixed workspace virtualenv and is narrow enough to use as a persistent
command-approval prefix. The manifest's first column must contain unique S3
URIs, and the mapping CSV must contain `s3_uri` and positive
`s3_size_bytes` columns.

1. Use the wrapper directly. Do not source shell startup files or unset
   credentials merely to run it. It uses the normal host credential chain
   available to its process.
2. Run the `plan` command. It checks the host AWS identity and reports total and
   pending image counts, decimal GB, binary GiB, free space, projected free
   space, destination, existing valid/invalid files, and a plan ID.
3. Present those values to the user and explicitly ask for approval.
4. Do not start a transfer from the same user request that asked for planning.
   Require a subsequent explicit approval tied to the displayed plan.
5. Run `download` only with the exact `--plan-id` and
   `--approved-download`. The script recomputes the plan and refuses if the
   manifest, mapping, AWS identity, destination, expected sizes, or pending set
   changed.
6. Downloads use temporary files and atomic replacement, validate exact sizes,
   and skip existing files only when their sizes match. Rerunning safely resumes
   pending work.
7. Never use pod or cluster AWS credentials as a workaround. If host
   authentication fails, ask the user to refresh it.

## Job Sequence

1. **Define the task and labels**
   - Confirm the task is appropriate for classification-style frontier
     annotation.
   - Copy a repository config to a job-specific location.
   - Make label definitions mutually understandable and request exactly one
     structured label.

2. **Collect inputs**
   - Use an existing local PNG directory, or create a CSV whose first column
     contains one `s3://...` image URI per row.
   - Inspect the mandatory S3 download plan and obtain explicit approval before
     executing it.
   - Keep the downloaded images and later labels on the same machine when
     practical.

3. **Create calibration data**
   - Select roughly 50–100 diverse examples.
   - Store the selected inputs and hand labels beneath the job root, for example
     `<job>/samples/calibration/images` and
     `<job>/samples/calibration/labels`.
   - Hand-label each image using a matching `<image-stem>.txt` file.
   - Include common cases, ambiguous cases, and rare but important labels.

4. **Prepare credentials and environment**
   - Never place credentials in chat, source control, configs, or command-line
     values.
   - Prefer `agent-secrets run --env` when a suitable keyring profile exists.
   - The annotation process expects `OPENAI_API_KEY`; the internal gateway setup
     also uses `OPENAI_BASE_URL`.
   - The S3 helper uses boto3's normal credential chain.
   - Installing dependencies or creating a virtual environment requires user
     approval when it changes the machine or downloads packages.

5. **Inspect, then calibrate**
   - Run the bundled runner's `inspect` command.
   - Confirm repository state, model, config, image count, dependency state, and
     output directory.
   - Obtain explicit approval, then annotate the calibration set with
     `--save-crops`.
   - Compare predicted text labels and crops against the hand labels.
   - Iterate on the job-specific config, crop behavior, model, and concurrency as
     needed. Do not silently modify `bg_ml`.

6. **Run the full dataset**
   - Re-run `inspect` against the full image directory.
   - Report the exact number of PNGs and the proposed output directory.
   - If saving crops, report host free space and projected output growth before
     starting. Prefer `average calibration crop bytes * remaining images`; when
     no calibration estimate exists, conservatively reserve the remaining
     source-image bytes.
   - Obtain fresh explicit approval for the billable run.
   - Run with `--approved-billable-run`. For a large run, begin with
     `--workers 4 --api-max-retries 5`; the runner modifies only its staged
     `/tmp` copy, not the source script in `bg_ml`.
   - Preserve partial outputs; the underlying script skips existing label files.

7. **Monitor and resume**
   - `status` is read-only and makes no model API calls:

     ```bash
     /home/ezekiel.flaton/.codex/skills/frontier-image-annotation/scripts/run_frontier_annotation.sh status \
       --images /path/to/images \
       --config /path/to/job-config.yaml
     ```

   - Report `completed_labels / total_images`, progress percentage, remaining
     labels, saved crop count, and the latest-label timestamp.
   - While Codex owns a live unified-exec session, poll that session and durable
     status. Do not rely on a separate `ps` alone because restricted process
     inspection may not show an escalated process.
   - If progress appears stalled, compare at least two status snapshots and poll
     the original session before declaring the job stopped. Never start a second
     copy while output files are advancing.
   - Measure throughput over at least several minutes after startup. If 4 workers
     are stable but still too slow, stop the process before testing 8 workers.
     Watch for stalled output or failures indicating a gateway or rate limit.
     Do not run overlapping processes against the same label directory.
   - Following a crash, run `status` first. If the process is gone, rerun the
     exact annotation command with the same images, config, model, and output
     directory. Existing labels are skipped, making the run resumable.
   - Prior approval remains applicable only to resuming that exact interrupted
     job. Ask again if the images, image count, config, model, or output
     destination changes.

8. **Review and save**
   - Verify input, label, and saved-crop counts before sampling.
   - Generate a deterministic local review gallery with
     `scripts/build_review_gallery.py`. By default it selects up to 25 examples
     from each observed label plus 150 additional globally random examples and
     writes the gallery and sample manifest beneath
     `<job>/annotation-review/`.
   - The gallery shows original images and labeled crops side by side, supports
     category and status filters, autosaves correct/incorrect/uncertain
     decisions in browser local storage, provides keyboard navigation, and
     exports review results to CSV.
   - Validate that sample stems are unique, every referenced original and crop
     exists, the expected sample count is present, and the generated JavaScript
     parses before handoff.
   - Use a heavier annotation tool such as Label Studio or CVAT only when the
     local gallery is insufficient for the review or collaboration needs.
   - Correct errors before treating labels as final.
   - With explicit permission, save final labels to the requested durable system
     such as Box, S3, or FiftyOne.

## Stop Conditions

Stop and report rather than improvising when:

- the requested task is not classification-like and no feasibility experiment
  has been accepted;
- the repository is missing, on an unexpected branch, or has relevant local
  changes whose intended use is unclear;
- required dependencies or credentials are unavailable;
- the selected config is invalid or has no labels;
- there are no PNG inputs;
- the user has not explicitly approved the billable model run;
- calibration quality is unacceptable and the user has not explicitly waived
  the calibration gate.
