# Output Format

## New Ticket Draft

Use this structure for `##TICKET`:

```markdown
Summary: <concise ticket title>
Project: RSPS
Issue Type: <SW Development Task or Bug>

### Background
- VR Date: <date>
- VRR Source: <link>
- Run Context: <site/system/station if relevant>
- Problem Summary: <plain-language summary>
- Impact: <why this matters>

### Technical Details
- Pick Inspector: <link>
- Observation: <technical behavior>
- Additional Evidence: <metrics, logs, screenshots, prior ticket refs, raw error strings>

### Definition of Done
- <concrete expected outcome>
- <validation or documentation requirement>
```

## Ticket Update Comment

Use this structure for `##TICKETUPDATE`:

```markdown
Target Ticket: <ticket key>

Comment Title: VRR Additional Evidence (<VR date>)

Comment Body:
- VR Date: <date>
- VRR Source: [Box VRR](<link>)
- Pick Inspector: [pickviewer](<link>)
- Problem Summary: <brief summary>
- Observation: <technical behavior>
- Additional Evidence: <metrics, screenshots, logs, related refs>
```

Do not modify the ticket description for `##TICKETUPDATE`. Keep the new VRR material isolated inside the Jira comment.
Use explicit Markdown link syntax in comment bodies rather than bare URLs so Jira renders the links as clickable.

## Source Document Status Annotation

Record completed work directly in the VRR source document under the original marker line.
Place the status line immediately below the marker itself, before any pick inspector link, screenshots, or blank spacer paragraphs.

Example for a new ticket:

```text
##TICKET Track down how nonitems are determined.
TICKET_STATUS: WRITTEN | 2026-03-12 | RSPS-1234 | Investigate non-item threshold logic | https://berkshiregrey.atlassian.net/browse/RSPS-1234
```

Example for an applied ticket update:

```text
##TICKETUPDATE Add this PIT example as additional context to the existing ticket.
TICKETUPDATE_STATUS: APPLIED | 2026-03-12 | RSPS-1234 | Investigate non-item threshold logic | https://berkshiregrey.atlassian.net/browse/RSPS-1234
```

When one of these status lines is present, treat the marker as already handled unless the user explicitly asks to overwrite or reapply it.
