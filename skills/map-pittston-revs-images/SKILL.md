---
name: map-pittston-revs-images
description: Map Pittston/Washington donor tote and product combinations to Robot Eligibility Vision System captures, prediction outcomes, and exact perception-logger PNG filenames. Use when Codex needs to trace `DonorToteId` plus `ProductId` pairs from `metrics_events` into `log.records`, build a table of tote/product to filename mappings, reconstruct Washington SKU values from `DistroNumber`/`PackSize`/`SizeCode`, follow the REVS manual prediction-analysis workflow, or prepare resumable Kubernetes image-copy manifests for the referenced PNG files.
---

# Map Pittston REVS Images

Map Pittston/Washington tote-product pairs from `pit-washington-metric_events*` to the corresponding REV scan, saved image filename, and eligibility decision in `pit-washington-log.records*`.

## Workflow

1. Read [references/pittston-washington.md](references/pittston-washington.md) before querying so the field names, logger names, and join order stay consistent.
2. Use `PickComplete` events in `pit-washington-metric_events*` as the source of truth for unique pairs.
3. Build unique `(DonorToteId, ProductId)` pairs first, or `(ToteID, DistroNumber, PackSize, SizeCode)` when `ProductId` is missing and Washington SKU reconstruction is required.
4. Join each tote to the REV capture in `pit-washington-log.records*` by finding the nearest `ImageBarcodeCameraUpstreamMessageSynchronizer` event for that tote and reading `latest_image_timestamp`.
5. Join the capture to the saved PNG by finding the nearest `/pick_scanner/perception_logger` line that starts with `Saved image topic /pick_scanner/rgb_camera/raw/image as `.
6. Join the same tote to `ImageBarcodeRobotEligibilityApplication` to collect `is_eligible` and `reason`.
7. Emit a table with one row per tote-product pair. Reuse the same PNG filename for multiple products in the same tote when the workflow shows only one tote-level capture.

## Download Phases

Always split image retrieval into two phases.

### Phase 1: Analysis And Deduping

- Read the CSV and deduplicate `png_filename` locally.
- Write the local manifest and state files first.
- Prefer `--manifest-only` when beginning the download workflow.
- Show the user the deduped counts, target output directory, and intended pod or container source.
- For Pittston runs, use `--context k8s/washington-pit-context` or rely on the script default.
- If time has passed or the pod has rotated, export the live pod directory listing and intersect it with the deduped manifest before starting downloads.

### Phase 2: Downloads

- Do not start downloads automatically after manifest generation.
- Check in with the user after phase 1 and wait for explicit confirmation before starting downloads.
- Use the resumable copy helper instead of generating one command per row.
- Default to `rsync` over `kubectl exec` so partial local files can resume instead of restarting from byte zero.
- Keep `kubectl cp` only as a fallback when `rsync` is not available.
- If `rsync` is missing on the pod, the operator can install it with `sudo apt-get update` and then `sudo apt install rsync`.
- Keep the state directory and result log updated after every file so interrupted runs can resume cleanly.
- If `kubectl` reports `pod not found`, resolve the current `perception-logger` pod name in the namespace before resuming.
- If `kubectl` reports credential failures like `You must be logged in to the server` or `the server has asked for the client to provide credentials`, stop the run immediately, refresh auth, and only then resume.
- If the live pod manifest overlap is zero, stop and report the blocker instead of churning through transfers.

## Bundled Scripts

Run `scripts/export_pittston_revs_table.py` through the existing Vault-backed Elasticsearch helper from the `bg-elasticsearch` skill:

```bash
/bin/bash /home/ezekiel.flaton@berkshiregrey.com/.codex/skills/bg-elasticsearch/scripts/run_bg_vault_elastic_python.sh \
  /home/ezekiel.flaton@berkshiregrey.com/.codex/skills/map-pittston-revs-images/scripts/export_pittston_revs_table.py \
  --date-from 2026-04-09 \
  --date-to 2026-04-13 \
  --output /tmp/pittston_revs_table.csv
```

For image retrieval, prefer `scripts/copy_revs_images.py`. It reads the CSV, deduplicates `png_filename`, writes local manifests, and then downloads the pending files with resumable `rsync` over `kubectl exec`.

Manifest-only phase:

```bash
/usr/bin/python3 /home/ezekiel.flaton@berkshiregrey.com/.codex/skills/map-pittston-revs-images/scripts/copy_revs_images.py \
  --csv /tmp/pittston_revs_table_2026-04-09.csv \
  --output-dir /tmp/pittston_revs_images_2026-04-09 \
  --manifest-only
```

Confirmed download phase:

```bash
/usr/bin/python3 /home/ezekiel.flaton@berkshiregrey.com/.codex/skills/map-pittston-revs-images/scripts/copy_revs_images.py \
  --csv /tmp/pittston_revs_table_2026-04-09.csv \
  --output-dir /tmp/pittston_revs_images_2026-04-09 \
  --context k8s/washington-pit-context \
  --namespace res1 \
  --pod perception-logger-58766bcd8c-bljcr
```

The copy script defaults:

- `--context k8s/washington-pit-context`
- `--transfer-mode rsync`
- `--rsync /usr/bin/rsync`
- `--remote-rsync /usr/bin/rsync`
- `--kubectl /usr/local/bin/kubectl`
- `--retries 999999` for `kubectl cp` fallback only

The copy script writes state under `<output-dir>/.copy-state` by default:

- `all_pngs.txt`
- `pending_pngs.txt`
- `copied_pngs.txt`
- `failed_pngs.txt`
- `results.jsonl`
- `kubectl_rsync_rsh.sh`

## Query Rules

- Prefer `ProductId` from `PickComplete`. For Washington, it already matches the expected SKU shape like `<DistroNumber>_<SizeCode>_<PackSize>`.
- Fall back to `ToteAssignmentMessage` only when `ProductId` is absent or when the user explicitly asks for the reconstructed SKU fields.
- Treat the REV capture as tote-level, not product-level. Multiple products in one tote can legitimately map to the same filename.
- Slice large date ranges into smaller windows when extracting `log.records`. Use local intermediate files so partial progress is retained when long-running queries or transfers fail.
- Before any download run, finish phase 1 and check in with the user.

## Output Shape

Prefer these columns unless the user asks for something else:

- `date`
- `donor_tote_id`
- `product_id`
- `pair_first_seen_ts`
- `sync_ts`
- `latest_image_timestamp`
- `png_filename`
- `prediction_ts`
- `is_eligible`
- `reason`

## References

- Use [references/pittston-washington.md](references/pittston-washington.md) for the exact index aliases, fields, logger names, Confluence page ID, and a verified mapping example.
