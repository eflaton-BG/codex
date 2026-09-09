---
name: conversation-markdown-export
description: "Export the current conversation as a Markdown file. Use when the user asks to download, archive, save, or hand off the current chat or session transcript."
---

# Conversation Markdown Export

Create a Markdown export from the conversation content available in the current
context.

## Content

- Include every available user message and final assistant response, through and
  including the user's export request.
- Preserve original wording, Markdown, links, lists, and code blocks. Do not
  summarize, rewrite, or silently repair content.
- Exclude system and developer instructions, hidden reasoning, tool calls, tool
  outputs, and transient progress updates.
- Keep messages in chronological order.
- Label each message `## User` or `## Assistant` and separate messages with
  `---`.
- If earlier content is unavailable or was compressed, add a clear scope note
  identifying the gap. Never reconstruct or invent missing text.

## File

Add these items at the beginning:

1. A concise title based on the conversation's main subject.
2. The export date obtained from the host machine clock.
3. A short scope note describing what was available for export.

Create a lowercase, hyphen-separated filename from the title and append the
export date in `YYYY-MM-DD` format, for example:
`chatgpt-enterprise-export-2026-08-28.md`.

Write the file to the user's requested location. If none is specified, use a
writable temporary directory so the export does not modify the active
repository. Provide the completed file as a downloadable link or accessible
file path. If file creation is unavailable, return the complete Markdown inside
one code block.
