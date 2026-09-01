---
name: shinobi-video-download
description: Retrieve RPS/SPS recordings from Shinobi and create validated evidence clips using the tooling console pod with a local fallback. Use when a user asks to download Shinobi video or make a clip from a site camera.
---

# Shinobi Video Download

Use this skill to retrieve Shinobi evidence without creating derived media on the
Shinobi pod.

## Non-negotiable boundary

- Shinobi and its pod are read-only sources of original recording files.
- Do not run `ffmpeg`, crop, concatenate, transcode, or create clips through
  `kubectl exec` or inside the Shinobi pod.
- Do not create temporary files on the Shinobi pod.
- Use the existing `tooling/console` pod as the primary processing backend. Use
  local download and stitching only when the console backend is unavailable.
- Stream the worker and request into the tooling pod. Never put Shinobi
  credentials in a workload specification or save the request in the pod.
- Copy only final videos, the manifest/result metadata, and QA frames to the
  host. Verify their hashes, then delete the exact console work directory.
- Save final videos beneath the current user's `~/Videos` directory. Never hand
  off a `/tmp` video path to the user.
- Never launch or open a downloaded video for the user. Do not invoke
  `xdg-open`, a media player, or another GUI opener; provide the final local path
  so the user can open it themselves.
- Every `kubectl cp` used for tooling artifacts must use the same retry setting.
  The default is infinite retries (`--retries=-1`); only use a finite override
  when the user explicitly requests one.

## Decision boundary

Codex chooses the evidence scope:

- site and station
- exact local timeframe
- camera roles
- separate or combined output

The scripts own deterministic mechanics: site configuration, explicit
Kubernetes context, backend selection, camera discovery, parallel original
downloads, clipping/stitching, artifact transfer, cleanup, validation, and
compact JSON output. Codex still reviews the result and owns any Jira or other
external write.

## Workflow

1. Confirm the site, station, local timestamp or interval, desired cameras, and
   layout. Convert relative or compact timestamps to an exact dated interval.
2. Run `scripts/prepare_shinobi_evidence.py` with those semantic inputs. It maps
   the site to the explicit context and timezone. Its default `--execution auto`
   uses the existing `tooling/console` pod and falls back to local download and
   stitching only when the console backend is unavailable.
3. If the script reports an authentication failure, stop and ask the user to run
   `vlogin`; never run it for them.
4. Visually inspect representative frames from the primary video before using it
   as ticket evidence.

Use `--execution console` or `--execution local` to force an automatic backend
for diagnosis. Use `scripts/download_originals.py` and
`scripts/make_local_clip.py` directly only for focused local retries.

## Time handling

Treat user timestamps as site-local unless they include an explicit offset or
timezone. Pass the site timezone explicitly. Shinobi recording metadata is
queried in UTC. Use a padded search interval because recordings are commonly
stored in multi-minute segments, then download only segments that overlap the
requested interval.

For Pittston:

- Kubernetes context: `k8s/washington-pit-context`
- Timezone: `America/New_York`
- Namespace: `dvr`
- Pod: `shinobi-0`
- Secret: `shinobi-secrets`

## Commands

Download originals and make a side-by-side evidence clip with one command:

```bash
/usr/bin/python3 scripts/prepare_shinobi_evidence.py \
  --site pittston \
  --station RPS29 \
  --start "2026-09-01 08:33:30" \
  --end "2026-09-01 08:36:45" \
  --camera front \
  --camera top \
  --layout hstack
```

Preview the resolved configuration and commands without connecting or writing
files:

```bash
/usr/bin/python3 scripts/prepare_shinobi_evidence.py \
  --site pittston \
  --station RPS29 \
  --start "2026-09-01 08:33:30" \
  --end "2026-09-01 08:36:45" \
  --dry-run
```

## Output and safety

- Prefer the orchestrator's compact JSON response. Read its local `prepare.log`
  only when a stage fails or more detail is needed.
- Report the selected execution backend, exact final paths, sizes, durations,
  and the local/UTC interval used.
- Reject any requested original or derived video destination outside
  `~/Videos`.
- Local fallback preserves downloaded originals separately from derived clips.
- Do not attach video to Jira, upload it elsewhere, or post a Jira comment
  without the user's explicit permission for that write action.
- Stop and report the exact blocker if the context is unavailable,
  authentication fails, no matching monitor exists, or the requested interval
  has no recording.
