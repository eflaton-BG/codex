---
name: rps-vrs-rotation
description: Use when deciding who should annotate the next VR in #rps-vrs, based on the VRR Rotation canvas and recent #rps-vrs activity. Reads the VRR Rotation canvas plus the #rps-vrs channel, recommends the next logical annotator in the rotation, gives the Slack link for the VR that was run, and drafts a Slack reply in Ezekiel Flaton's direct assignment tone. Never writes to Slack.
---

# RPS VRS Rotation

Use this skill when the user wants to know who should take the next VR annotation in `#rps-vrs`, or wants a Slack-ready assignment message for that next person.

## Read-Only Rule

This skill is read-only for Slack.

- Allowed Slack tools: `slack_read_canvas`, `slack_search_public`, `slack_read_thread`, `slack_read_channel`
- Do not use: `slack_send_message`, `slack_send_message_draft`, `slack_schedule_message`, `slack_create_canvas`

## Fixed Sources

- Channel: `#rps-vrs`
- Channel ID: `C0AH03Q6R4Z`
- Rotation canvas title: `VRR Rotation`
- Rotation canvas ID: `F0B08JZF21E`

Always reread the canvas each time. Do not rely on stale chat context because the annotator list may have changed.

## Known Annotators

- `MB` = Malhar Bhoite = `<@U01UZ84CKH6>`
- `AA` = Abinav Anantharaman = `<@U05DLSQMGP5>`
- `DC` = Danny Chua = `<@U01KUDHJMLY>`

If the canvas contains a new set of initials that is not listed above, report that explicitly instead of guessing.

## Workflow

1. Read the `VRR Rotation` canvas.
2. Parse the entries as the authoritative rotation history. Treat the date in each line as the run date, not the presentation date.
3. Search `#rps-vrs` for the newest VR run that still needs an assignee or that the user is asking about.
4. Capture the direct Slack link for that VR run message or thread starter.
5. Determine the next logical annotator.

### Choosing The Next Annotator

Use this order of precedence:

1. Prefer the current repeating cadence visible in the most recent canvas entries.
2. If the recent entries form a clear cycle, continue that cycle.
3. If the cycle is ambiguous, choose the least recently assigned annotator among the known annotators in the canvas.
4. If a candidate is clearly unavailable in-channel today, such as saying they are OOO, skip them and move to the next person.

For example, if the recent canvas entries are `MB -> AA -> DC -> MB -> AA`, the next logical person is `DC`.

## What To Return

Return exactly two items:

1. `VR + Assignee`
Give:
- the run description
- the Slack link for the VR that was run
- the recommended assignee
- a one-line reason

2. `Slack Draft`
Give a short message the user can paste into `#rps-vrs`.

## Tone Rules For The Draft

Match Ezekiel Flaton's style:

- direct
- concise
- operational
- little or no small talk
- no extra enthusiasm
- explicitly name the run date and site when known
- prefer simple assignment language like `You're on...`, `you're up...`, or `can you take...`

Read [references/voice.md](references/voice.md) before drafting.

## Draft Shape

Prefer one of these patterns:

- `<@user> you're up for the Thursday VRR. Can you annotate today's 4/21 run?`
- `Plan for the VRR today`
  Then one short assignment per line.
- `For the Thursday VRR:`
  Then one short assignment per line.

Do not over-explain the reasoning in the Slack draft. Keep the reasoning in the private answer to the user, not in the pasted Slack text.

## Notes

- The canvas is the source of truth for who has already been used in the rotation.
- The channel is the source of truth for the current VR link and near-term availability.
- If there are multiple fresh VRs, give one recommendation per VR unless the user asked for only one.
