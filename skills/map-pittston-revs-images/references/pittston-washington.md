# Pittston/Washington REV Mapping

## Datasets

- `pit-washington-metric_events*`
- `pit-washington-log.records*`

## Confluence Reference

- Page: `REVS - Feature Testing`
- Page ID: `2849898503`
- Relevant section: `Prediction Analysis using manual steps (Use only when Robot Eligibility Inspector is down)`

## Metrics Source of Truth

Use `pit-washington-metric_events*` `PickComplete` as the direct source for tote-product pairs.

Important fields:

- `EventType.keyword = PickComplete`
- `DonorToteId.keyword`
- `ProductId.keyword`
- `DistroNumber`

Use `ToteAssignmentMessage` only when the user needs the Washington SKU rebuilt from component fields.

Important fields:

- `EventType.keyword = ToteAssignmentMessage`
- `ToteID.keyword`
- `DistroNumber`
- `PackSize`
- `SizeCode.keyword`

Washington SKU reconstruction:

- `ProductId` already follows the site SKU shape.
- Reconstruct with `<DistroNumber>_<trim(SizeCode)>_<PackSize>` when needed.
- Example: `2707559_VP_6`

## REV Log Join Points

Use `pit-washington-log.records*` for the capture, saved PNG, and prediction.

Capture/sync event:

- Logger: `ImageBarcodeCameraUpstreamMessageSynchronizer`
- Fields: `tote_id.keyword`, `latest_image_timestamp`

Scan event:

- Logger: `ConsoleUpdatePublisher(RES1_robot_eligibility)`
- Useful for confirming the tote hit the REV workflow.

Saved PNG event:

- Logger: `/pick_scanner/perception_logger`
- Match lines that start with:
  `Saved image topic /pick_scanner/rgb_camera/raw/image as `

Prediction event:

- Logger: `ImageBarcodeRobotEligibilityApplication`
- Fields: `is_eligible`, `reason`
- Tote can appear in the log message for the completed prediction pipeline.

## Join Order

1. Get unique `(DonorToteId, ProductId)` pairs from `PickComplete`.
2. Keep the earliest timestamp per pair as `pair_first_seen_ts`.
3. For each tote, find the nearest `ImageBarcodeCameraUpstreamMessageSynchronizer` event and record:
   - `sync_ts`
   - `latest_image_timestamp`
4. Find the nearest `/pick_scanner/perception_logger` save event around the sync and extract the PNG path.
5. Find the nearest `ImageBarcodeRobotEligibilityApplication` event for the same tote and record:
   - `prediction_ts`
   - `is_eligible`
   - `reason`

## Volume Workaround

Some Pittston/Washington days exceed Elasticsearch's normal `10000` hit window for sync, save, and prediction events.

Use smaller time windows for `log.records` extraction when needed:

- Default: six-hour slices
- Combine the slice-level results after extraction
- Avoid relying on a single full-day hit dump when totals are at or above `10000`

## Known Caveat

The image capture is tote-level. Different `ProductId` values from the same tote can map to the same `png_filename` when the REV workflow captured the tote once.

## Verified Example

- `date`: `2026-04-13`
- `donor_tote_id`: `R:05415`
- `product_id`: `2707233_VP_6`
- `sync_ts`: `2026-04-13T15:18:00.776Z`
- `latest_image_timestamp`: `2026-04-13 15:18:00.313934+00:00`
- `png_filename`: `2026-04-13/69dd09280556ed8c830cf73c.png`
- `prediction_ts`: `2026-04-13T15:18:01.352Z`
- `is_eligible`: `true`
- `reason`: `product_category`
