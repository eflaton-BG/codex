---
name: vrr-ticket-authoring
description: Draft Jira ticket content from Volume Run Review notes, slide decks, pasted observations, and Box-hosted VRR documents. Use when Codex needs to convert `##TICKET` markers into new Jira-ready ticket drafts, convert `##TICKETUPDATE` markers into Jira comment payloads for existing tickets, or extract structured VRR evidence such as run date, VRR source link, pick inspector links, observations, notes, screenshots, and related references.
---

# Vrr Ticket Authoring

Extract Jira-ready ticket material from VRR content without inventing missing context. Treat inline ticket markers as instructions attached to the full observation block above them, not as standalone one-line requests.

Use this skill when working from:
- Box VRR links
- Pasted VRR notes
- Confluence/wiki VRR pages
- Raw observation logs that include `##TICKET` or `##TICKETUPDATE`

Read [references/output-format.md](references/output-format.md) when drafting the final ticket body or Jira comment payload.

For Box-hosted VRRs, always prefer the prefix-approvable wrapper script instead of rebuilding the shell command inline:
- `scripts/extract_box_vrr_to_file.sh <box-file-url-or-id> <output-file>`
- This wrapper delegates to `scripts/extract_box_vrr.sh`, which resolves the Box file ID, loads the local `mcp-server-box` repo environment, and uses writable `/tmp` cache/config directories.
- After writing the extracted text to the requested file, read that file for parsing instead of depending on shell redirection.
- For Box `.docx` status writeback, use `scripts/update_box_vrr_status.sh` directly instead of rebuilding the Box download/upload command inline.
- Preferred writeback flow:
  - write a temporary JSON plan file containing `marker` and `status_line` entries
  - run `/home/ezekiel.flaton@berkshiregrey.com/.codex/skills/vrr-ticket-authoring/scripts/update_box_vrr_status.sh apply <box-file-url-or-id> <plan-json>`
  - if you need to inspect the current marker paragraphs first, run `/home/ezekiel.flaton@berkshiregrey.com/.codex/skills/vrr-ticket-authoring/scripts/update_box_vrr_status.sh inspect <box-file-url-or-id>`

## Parse The VRR

Identify every `##TICKET` and `##TICKETUPDATE` marker in the source material.

For each marker:
1. Start at the marker line.
2. Walk upward and capture the full observation block that belongs to that marker.
3. Include all relevant context above the marker before the previous unrelated observation or run header begins.
4. Treat comments at the bottom of the observation as part of that observation.
5. Preserve exact URLs and notable raw evidence such as stack traces, pick IDs, perception IDs, screenshots, and station names.

Use nearby run headers to capture run metadata such as:
- VR date
- site or environment such as `PIT` or `ITF`
- station or system identifiers
- run timing and top-line metrics when relevant

## Required Evidence

Every new ticket draft and every ticket update comment payload must include:
- VR date
- VRR source link
- pick inspector link
- observation summary
- relevant notes/comments attached to that observation
- any other materially relevant evidence from the observation block

If one of these is missing:
- do not invent it
- state the gap explicitly
- ask for the missing input or flag the draft as incomplete

## Handle `##TICKET`

Treat `##TICKET` as a request to draft a new Jira issue from the observation block above it.

Default authoring policy:
- use project `RSPS` unless the user explicitly requests another project
- use issue type `SW Development Task` or `Bug`
- include all three required description sections:
  - `### Background`
  - `### Technical Details`
  - `### Definition of Done`

Map the evidence as follows:
- `### Background`: user-visible problem statement, run context, impact, VR date, VRR source link
- `### Technical Details`: pick inspector link, raw observation details, system behavior, metrics, logs, screenshots, related references
- `### Definition of Done`: concrete investigation or implementation outcomes implied by the marker text

Do not collapse raw evidence into vague prose. Keep links and exact error strings when they matter.

If the source document already contains a completion marker for that `##TICKET` block, do not draft or create a duplicate ticket. Surface the existing ticket reference instead.

## Handle `##TICKETUPDATE`

Treat `##TICKETUPDATE` as a request to add a Jira comment to an existing ticket referenced in that section.

Do not create a new ticket for `##TICKETUPDATE`.

When updating:
1. identify the target Jira ticket from the local section
2. preserve the existing ticket narrative by leaving the description unchanged
3. draft the VRR-derived material as a standalone Jira comment
4. do not rewrite or interleave the new context into the existing description paragraphs

Format Jira comment links as explicit Markdown links so they render as clickable links in Jira comments.
Prefer labels such as:
- `VRR Source: [Box VRR](<url>)`
- `Pick Inspector: [pickviewer](<url>)`
- `Related Ticket: [RSPS-1234](<url>)`

Use a short comment heading that makes the source explicit, for example:
- `VRR Additional Context (<VR date>)`
- `VRR Additional Evidence (<VR date>)`

Put the new material in that comment only:
- VR date
- VRR source link
- pick inspector link
- concise observation summary
- additional notes, logs, screenshots, or related references from the VRR

If the target Jira ticket is not clearly identified, stop and ask for clarification instead of guessing.

If the source document already contains a completion marker for that `##TICKETUPDATE` block, do not apply the same update again unless the user explicitly asks for a repeat.

## Preserve Ordering

When drafting from a marker, keep the supporting evidence ordered from broad context to narrow evidence:
1. VR date and run/site context
2. VRR source link
3. observation summary
4. pick inspector link
5. other relevant details such as screenshots, metrics, logs, existing ticket references, and named follow-ups

This ordering matters because the marker comment is usually at the bottom of the observation, and the ticket should reflect the full evidence above it.

## Output Rules

Prefer concise, Jira-ready prose over verbatim transcription.

When mentioning Jira tickets in the response:
- include the ticket key, summary, and direct browser URL on the same line
- use clickable Markdown links when possible

Treat a user request to process a VRR, do the same with a VRR, or run the VRR workflow as standing authorization to:
- read the VRR source
- create Jira tickets for `##TICKET`
- update existing Jira tickets for `##TICKETUPDATE`
- edit the source VRR to insert status markers

Only stop to ask for clarification if the target Jira ticket or source document is genuinely ambiguous and proceeding would risk writing to the wrong place.

## Source Document Idempotency

After a successful Jira write, update the source VRR document so later runs can detect that the work was already completed.

Keep the original marker line. Add the status line directly under the marker line, before any links, screenshots, blank lines, or other evidence, using this format:

For new tickets:
```text
TICKET_STATUS: WRITTEN | <YYYY-MM-DD> | <TICKET-KEY> | <summary> | <url>
```

For ticket updates:
```text
TICKETUPDATE_STATUS: APPLIED | <YYYY-MM-DD> | <TICKET-KEY> | <summary> | <url>
```

Before drafting or writing from a marker:
- check the marker block for one of these status lines
- if present, treat the block as already handled
- report the recorded ticket instead of generating a duplicate

Only add the status line after the Jira create or comment succeeds.

If the source document cannot be edited in the current environment, stop after the Jira write and tell the user the ticket was created but the source document still needs its status marker for idempotency.

## Box Document Update Workflow

When the VRR source is a Box `.docx` and a status marker must be written back:
- prefer `/home/ezekiel.flaton@berkshiregrey.com/.codex/skills/vrr-ticket-authoring/scripts/update_box_vrr_status.sh apply <box-file-url-or-id> <plan-json>` for the entire writeback and verification flow
- use `/home/ezekiel.flaton@berkshiregrey.com/.codex/skills/vrr-ticket-authoring/scripts/update_box_vrr_status.sh inspect <box-file-url-or-id>` when you need to confirm the exact marker paragraphs before writing
- keep the plan file entries exact:
  - `marker`: the full marker paragraph text, including the original `##TICKET` or `##TICKETUPDATE` line
  - `status_line`: the full `TICKET_STATUS: ...` or `TICKETUPDATE_STATUS: ...` line to insert
- the wrapper handles local download, paragraph insertion, clean `.docx` rebuild, Box version upload, and post-upload verification
- if the Box version upload fails unexpectedly, ask the user to check whether the Box document is locked before concluding the token or file permissions are wrong
- verify recent `.docx` writes through the wrapper's re-download step rather than relying on Box text extraction alone
- do not rely only on `box_file_text_extract(...)` for immediate post-upload verification because Box text extraction can lag behind the latest uploaded version
