# Box Workbook Update

## Retrieve the Current Version

Use the Box skill's authenticated MCP path when it supports the required operation.
If MCP cannot download or upload the binary, the `bga-connections` skill provides the
connected Box gateway path:

1. List connections, find `box:default`, and inspect permissions using its current
   concrete connection UUID. Do not hard-code a UUID or pass the ref to provider calls.
   BGA authentication is independent of `vlogin`.
2. Verify the actor and read `/files/2362022399890`, requesting the live name,
   `file_version`, `etag`, `sha1`, size, permissions, and lock.
3. Check download/upload access and respect locks. Download the exact version read
   above with `download` on `/files/2362022399890/content` and query
   `version=<file_version.id>`. Preserve the source file and verify its SHA1.

Do not rely on an old local workbook or a hard-coded version/ETag. The file's name may
still say July even when adding reports from later months; do not rename it.

## Extend the Daily-Sheet Convention

Inspect and render an existing populated daily sheet before editing. Match its established
layout rather than redesigning the workbook:

- `A:H`: the CSV's eight original headers and data, starting with headers in row 1.
- `J1:J5`: `Distros Completed`, `Picks Completed`, `Average Distro Time (sec)`,
  `Average Distro Time (hr)`, `Picks per Distro`.
- For last data row `N`, use these formulas in `K1:K5`:
  `=COUNT(A2:AN)`, `=SUM(H2:HN)`, `=AVERAGE(G2:GN)`, `=K3/3600`, `=K2/K1`.
  Here `N` means the actual row number: for 1,290 distros, the first formula is
  `=COUNT(A2:A1291)`. Do not write a literal `N` into Excel.
- If the CSV has no distros, create a header-only data area, set distro and pick totals
  to zero, and leave averages and picks-per-distro unavailable rather than dividing by zero.
- Use actual numbers for distro IDs, seconds, and pick counts. Missing timestamps and
  durations stay blank; preserve valid zero durations and zero pick counts.
- Keep columns `B:F` as literal text. With artifact-tool, set their number format to `@`
  **before** assigning values; otherwise ISO timestamp strings can become Excel serial
  numbers and lose their displayed UTC/New York offsets. Verify exact text against the CSV.
  Retain `TotalPickTime` text such as `60:08:12.956`, without wrapping at 24 hours.

For a run covering November 6–12, the leading tab order is:
`Overview`, `20261112`, `20261111`, ..., `20261106`, then other existing tabs in their
previous relative order. Do not create duplicate names or silently overwrite differing data.

## Preserve and Validate

Use the spreadsheets skill's artifact-tool authoring workflow. Check saved results, not just
the in-memory model. A full workbook export must not silently modify unrelated sheets or
native features. Where full round-tripping would risk that, author the new sheets separately
with artifact-tool and integrate their OpenXML parts into a copy of the original package:

- Preserve existing worksheet parts and their relationships byte-for-byte.
- Allocate unique worksheet part names, sheet IDs, and relationship IDs.
- Merge new style records with remapped font/fill/border/number-format/XF indexes while
  preserving existing style indexes. Inline new shared strings or remap them correctly.
- Change only required package metadata, styles, and sheet ordering; keep other parts intact.
  Preserve namespace declarations, existing formula caches, and relationships.
- If sheet indexes change, preserve index-based workbook view or defined-name references.

Recalculate new summary formulas once, check their results independently, and render every
new sheet's headers, sample rows, and summary block. Verify all source cells against the CSVs,
unique distro counts, summed picks, elapsed durations, missing-versus-zero values, and tab order.
Check that no new formula errors were introduced. Existing unrelated formula errors are not
permission to repair or flatten their formulas.

## Upload and Verify

Immediately before upload, reread the live metadata. If the version or SHA1 changed, rebuild
on the new source rather than overwriting someone else's work.

For BGA, use the permitted update-content endpoint:
`POST https://upload.box.com/api/2.0/files/2362022399890/content`.
Supply `If-Match: <current-etag>`, multipart text
`attributes={"name":"<live file name>"}`, and the validated workbook as multipart `file`.
For MCP binary version uploads, use `get_upload_url` with the existing `file_id`.

After uploading, read the same file with the same actor and verify the saved version,
size, and SHA1 against the local workbook. Preserve its existing sharing and folder.
If a write times out or returns an ambiguous error, inspect version/SHA1 before any retry;
do not blindly replay a write that may have succeeded. On a conflict, denied permission, or
repeated failure, retain the validated local output and report the specific blocker.
