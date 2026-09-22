# Pittston RES1 August 2026 Findings

This is dated operational evidence, not a universal retention or outage rule.

## Sources and Join

- Prefer `pit-washington-metric_events*` events where
  `EventType.keyword=SkuRobotEligibilityChange`, `Source.keyword=RES`, and
  `StationId.keyword=RES1`.
- Deduplicate non-empty `SkuId.keyword` values for the requested local range.
- `RobotEligibilityPredictionStats` contains SKU, tote, prediction time, and
  `image_msg_timestamp`, but not the persisted S3 image path.
- The verified Elasticsearch path join used RES1 prediction-pipeline logs,
  `ImageBarcodeCameraUpstreamMessageSynchronizer.latest_image_timestamp`, and
  `/pick_scanner/perception_logger` RGB-save messages.
- Validate selected paths against the host-visible S3 inventory and reject
  missing or zero-byte objects.

Any use of prediction statistics, prediction logs, `PickComplete`, or MongoDB as
the SKU source is a fallback and requires explicit user approval first.

## Verified August Window

- August 1, 2026 had no complete, usable RES1 prediction rows in the extracted
  data; this does not prove that no prediction activity occurred.
- The first complete usable row was August 2, 2026 at 02:22:06 EDT.
- The first zero-byte object occurred August 23, 2026 at 06:10:56 EDT
  (`2026-08-23T10:10:56Z`).
- The pre-outage window contained 32,102 unique RES1 SKUs.
- 31,362 SKUs had distinct, validated, non-zero S3 image paths totaling
  103,200,696,326 bytes (103.201 GB / 96.113 GiB).
- 740 SKUs had no recoverable saved image. Their events were concentrated on
  August 4, when the perception logger reported `Unable to save image` instead
  of recording a PNG filename.

## Durable Local Artifacts

- Consolidated job directory:
  `/home/ezekiel.flaton/Downloads/pittston-res1-august-2026`
- Manifest directory:
  `/home/ezekiel.flaton/Downloads/pittston-res1-august-2026/pittston-res1-august-2026-sku-image-manifest`
- Unique SKUs:
  `/home/ezekiel.flaton/Downloads/pittston-res1-august-2026/pittston-res1-august-2026-sku-image-manifest/unique-skus.csv`
- One image per mapped SKU:
  `/home/ezekiel.flaton/Downloads/pittston-res1-august-2026/pittston-res1-august-2026-sku-image-manifest/one-image-per-sku.csv`
- S3 paths:
  `/home/ezekiel.flaton/Downloads/pittston-res1-august-2026/pittston-res1-august-2026-sku-image-manifest/s3-paths.txt`
- Downloaded images:
  `/home/ezekiel.flaton/Downloads/pittston-res1-august-2026/pittston-res1-august-2026-images`
- Frontier labels and crops:
  `/home/ezekiel.flaton/Downloads/pittston-res1-august-2026/pittston-res1-august-2026-images_labels_gpt-5.6-sol`
- Category-distribution graph:
  `/home/ezekiel.flaton/Downloads/pittston-res1-august-2026/Product Category Distribution August 2026.png`
- Review gallery:
  `/home/ezekiel.flaton/Downloads/pittston-res1-august-2026/annotation-review/index.html`
- Review sample manifest:
  `/home/ezekiel.flaton/Downloads/pittston-res1-august-2026/annotation-review/sample-manifest.csv`

Monitor the annotation without API calls:

```bash
/home/ezekiel.flaton/.codex/skills/frontier-image-annotation/scripts/run_frontier_annotation.sh status \
  --images /home/ezekiel.flaton/Downloads/pittston-res1-august-2026/pittston-res1-august-2026-images \
  --config /home/ezekiel.flaton/devel/bg_ml_ws/bg_ml/experiments/robot_eligibility_classifier/configs/product_annotation.yaml
```

If the machine or process crashes, run `status` first. Do not start another
process while the label count or latest-label timestamp is advancing. If the
job stopped, resume the exact approved job:

```bash
/home/ezekiel.flaton/.codex/skills/frontier-image-annotation/scripts/run_frontier_annotation.sh annotate \
  --images /home/ezekiel.flaton/Downloads/pittston-res1-august-2026/pittston-res1-august-2026-images \
  --config /home/ezekiel.flaton/devel/bg_ml_ws/bg_ml/experiments/robot_eligibility_classifier/configs/product_annotation.yaml \
  --workers 4 \
  --api-max-retries 5 \
  --save-crops \
  --approved-billable-run
```

The annotation script skips existing labels. This resume authorization applies
only while the images, config, model (`gpt-5.6-sol`), count (31,362), and output
directory remain unchanged.

Regenerate the 500-image review sample and local browser gallery with:

```bash
/home/ezekiel.flaton/devel/colcon_ws/src/.venv/bin/python \
  /home/ezekiel.flaton/.codex/skills/frontier-image-annotation/scripts/build_review_gallery.py \
  --job-dir /home/ezekiel.flaton/Downloads/pittston-res1-august-2026 \
  --images-dir /home/ezekiel.flaton/Downloads/pittston-res1-august-2026/pittston-res1-august-2026-images \
  --labels-dir /home/ezekiel.flaton/Downloads/pittston-res1-august-2026/pittston-res1-august-2026-images_labels_gpt-5.6-sol \
  --graph "/home/ezekiel.flaton/Downloads/pittston-res1-august-2026/Product Category Distribution August 2026.png" \
  --title "Pittston RES1 — August 2026 Annotation Review" \
  --dataset-id pittston-res1-august-2026
```

The sample is deterministic: up to 25 images from each observed category plus
150 additional globally random images, with seed `202608`. It currently contains
500 unique SKU/image stems across 14 observed labels. Keep the gallery and its
sample manifest inside the consolidated job directory.

## Durable Regeneration

Do not retain the final manifest only in `/tmp`. Use
`scripts/run_export_res1_sku_images.sh` with a persistent output directory.
The exporter:

1. records that the metric-first RES1 query was attempted;
2. uses the explicitly approved `RobotEligibilityPredictionStats` Atlas
   fallback for the exact RES1 SKU and image timestamps;
3. joins those timestamps to
   `ImageBarcodeCameraUpstreamMessageSynchronizer` and
   `/pick_scanner/perception_logger` in Elasticsearch;
4. validates that each selected S3 object exists and is non-zero;
5. preserves daily intermediate files and S3 HEAD results under `.work`;
6. writes the unique SKU list, mapping CSV, S3 path manifest, unmatched list,
   and summary outside `/tmp`.

The Elasticsearch stages use durable sub-day files under each date directory;
rerun the same command after a timeout to reuse complete days and complete
sub-day slices.

Do not infer an S3 key from ObjectId timestamp proximity when a save record is
missing; validation against known mappings showed that inference was not
reliable enough for exact SKU-to-image assignment.
