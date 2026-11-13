---
name: pittston-distro-pick-times
description: Generate daily Washington/Pittston distro pick-time CSVs and add the reports as newest-first sheets in the established Box Excel workbook. Use for a requested date or date range, joining each day's DistroComplete distros to PickComplete activity from that day and the preceding four calendar days. Supports CSV-only requests.
---

# Pittston Distro Pick Times

Generate each report with the bundled script instead of rebuilding the Elasticsearch queries.
The default deliverables are daily CSVs plus a new version of the established Box Excel workbook.
Skip the workbook update for CSV-only, dry-run, or read-only requests.

## Input

Resolve the requested date or date range in `America/New_York`; run the script once per date.
For "the last week," use the last seven completed New York calendar days, excluding today,
unless the user specifies a different week. Read the host clock and state the exact dates.

- Distro window: selected date at 00:00 inclusive through the next local midnight exclusive.
- Pick window: four local calendar days before the selected date at 00:00 through the next midnight after the selected date, exclusive.
- Convert both local windows to UTC before querying. This preserves correct 23-hour or 25-hour windows across daylight-saving transitions.

## Run

Before a live query, state the resolved alias and both local/UTC windows.
First confirm the existing `vlogin` session with a bounded, non-authenticating preflight;
never run `vlogin` for the user. If it is absent, expired, or cannot be confirmed, stop and
ask the user to refresh it.

From this skill directory:

```bash
bash scripts/run_bg_vault_elastic_python.sh \
  scripts/query_pittston_distro_pick_times.py \
  --date 2026-07-21
```

Use `--output /path/report.csv` when the user specifies a destination. Otherwise, write
`pittston_distro_pick_times_YYYY-MM-DD.csv` in the caller's current directory.

Use `--dry-run` to validate date boundaries without accessing Vault or Elasticsearch or
updating Box.

## Update the Excel Workbook

After validating the CSVs, follow [the workbook update procedure](references/box-workbook.md).
Read the `spreadsheets` and `box` skills for their artifact and provider workflows.

- Box file ID: `2362022399890`.
- Browser URL: https://berkshiregrey.app.box.com/file/2362022399890
- Current name: `DistroCompletion - July 2026.xlsx`; read the live name and retain it.
- Upload a **new version of this same file**, not a new file or a renamed workbook.
- Name daily sheets `YYYYMMDD`. Keep `Overview` first, then place this run's daily
  sheets immediately after it in descending date order: newest furthest left.
  Preserve the relative order of other existing tabs.
- Preserve existing sheets, including Overview content, formulas, formatting, and native
  features. Do not update Overview rows or charts unless separately requested.
- Retain all eight CSV columns and every distro, including no-pick distros.
  Preserve timestamp offsets and durations over 24 hours.
- Avoid duplicate date sheets. Reuse an existing date sheet only after verifying its
  contents match; if they differ, ask before replacing unless replacement was requested.

An execution request for this end-to-end report includes the described workbook update,
subject to current tool permissions. Merely reading or editing the skill does not authorize
an upload. A CSV-only request, different destination, or other explicit limitation takes
precedence. Do not infer permission for unrelated Box changes.

## Query Contract

- Vault cluster: `elastic-washington-cluster`
- Elasticsearch alias: `pit-washington-metric_events-alias`
- Time field: `@timestamp`
- Distro event types: `DistroComplete` and `DistroCompleteMessage`
- Pick event type: `PickComplete`
- Distro key: `DistroNumber`

Deduplicate the paired distro-complete event types by `DistroNumber`. Keep every completed distro
in the CSV, including distros with no matching picks.

Calculate `TotalPickTime` as the elapsed interval from the first to the last `PickComplete` for the
distro. A distro with one pick has zero elapsed pick time. Do not sum per-message processing fields.

## Output

The CSV contains:

- `DistroNumber`
- `FirstPickUTC`
- `LastPickUTC`
- `FirstPickNewYork`
- `LastPickNewYork`
- `TotalPickTime`
- `TotalPickTimeSeconds`
- `PickCompleteCount`

After execution, report the exact local and UTC windows, unique distro count, distros with and
without picks, and CSV output paths. For a workbook update, also provide its direct Box URL,
the verified saved version, and the added sheet names/order.

## Failure Handling

- Use the Vault-provided `.es.` endpoint, never a Kibana `.kb.` URL.
- If expected fields are missing, inspect field capabilities before changing the query.
- If no distros complete during the selected date, still create a header-only CSV.
- Do not omit no-pick distros or silently widen either date window.
- If a workbook update fails, retain the validated CSVs and local workbook, report the
  upload as pending, and do not rerun successful Elasticsearch queries.
