---
name: artifact-template-bg-team-presentations
description: "Create a presentation using the BG Team Presentations template and its retained reference file. Use when the user selects this template, names BG Team Presentations, or explicitly invokes $artifact-template-bg-team-presentations. Create Berkshire Grey team presentations using the BG template, native bullet points, and Zeke’s practical speaker-note voice."
---

# BG Team Presentations

Create a presentation from this template. Keep the reference file unchanged.

## Workflow

1. Read `artifact-template.json` and resolve its paths relative to this skill directory.
2. Load [@presentations](plugin://presentations@openai-primary-runtime) and invoke its reference/template workflow with the retained file.
3. Treat the user's prompt and available sources as the content input. Do not invent facts merely to fill a template slot.
4. Clone or import the reference instead of replacing its visual system with generic defaults.
5. Render and verify the finished presentation, then return the final artifact.

## Fidelity

Preserve source slides, layouts, masters, typography, geometry, images, charts, tables, and recurring slide chrome.

User instructions control requested content and explicit deviations. The retained reference controls layout and formatting where the user has not requested a change.

## Zeke's presentation preferences

- Propose slide order, purpose, and contents before doing slide work for a new
  deck. Once approved, execute; preserve subsequent feedback.
- Default to a focused 10–20 minute talk, never over 30 minutes unless requested.
  Use a timing estimate in speaker notes rather than crowding slide content.
- Use **native PowerPoint bullet points** for body lists, outline items, and
  conclusion recaps, not unmarked lines or typed bullet characters. Use consistent
  hanging indentation and spacing. Keep topic labels as headings, with supporting
  points underneath. Titles, dividers, captions, and Thank You need not be bullets.
- Keep slide bullets short: key phrases rather than full explanatory sentences.
  Preserve the mechanism and why it is interesting; move qualifications and full
  explanations into natural speaker notes. For research recaps, aim for two
  compact bullets per topic, typically 5–12 words when meaning permits.
- Center image-caption text beneath its image. Check the rendered alignment,
  not merely the caption box position.
- Use actual retained BG template slides for cover, outline, section dividers,
  content, conclusion, and Thank You. Do not redraw approximations. Preserve
  branding, layouts, masters, footer treatment, slide size, and editable objects.
- Add dividers between the major sections and end with the template Thank You.
  Label the recap **Conclusion**, not Main Takeaways. For conference recaps,
  organize the outline as takeaways, interesting ideas, and conclusion; the IROS
  deck specifically used **Four Takeaways / Interesting Ideas / Conclusion**.
- Do not add feedback requests, audience decision prompts, assigned owners, or
  a roadmap unless explicitly requested. A research question is not an audience
  feedback request.
- User-designated takeaways anchor the talk. Treat other research as interesting
  things that may or may not factor into our work, not recommended priorities.
- Use source figures and user-provided photos where they clarify a point. Keep
  figure data and labels intact. Separate published results from our proposed
  applications, informal observations, and unverified assumptions.

## Research figures and revision lessons

- Two papers can share a slide when each has its own heading, two short native
  bullets, and a readable source figure. Remove a redundant standalone slide when
  moving its topic into a pair; update notes, source references, and slide numbers.
- Match the image to the takeaway: a labeled gripper/sensor setup can communicate
  a hardware opportunity better than a dense method diagram or results table.
  Inspect the supplied PDF rather than assuming its first figure is the best.
- Preserve figure labels and aspect ratio; record figure number and PDF page.
  Keep generated visual prompts distinct from measured evidence in speaker notes.
- For panorama stitching artifacts, prefer a tighter crop of useful real content
  and modest exposure correction over fabricating missing details. Keep originals.
- Apply user-corrected product capitalization consistently in captions, titles,
  recaps, and speaker notes, not just the initially identified slide.

## Speaker-note voice

Use first-person, natural speaking notes in Zeke's voice:

- Direct, practical, specific, and operator-oriented; concrete system and goal first.
- Prefer "Goal is to…", "We noticed…", "This would let us…", and
  "Likely also want…" when natural, without repeating stock phrases everywhere.
- State what matters for our systems and why. Preserve exact technical names.
- Avoid corporate polish, process ceremony, motivational framing, and inflated
  confidence. Do not write presenter instructions such as "Explain that…".
- Notes are a speaking track, not a duplicate of the bullets. Natural prose is
  appropriate; use bullets for separate talking points when useful.
- Put URLs, paper titles, figure/page references, and source caveats in a separate
  **SOURCES (reference, not spoken)** section, not in the spoken script.

Voice adapted from the user's Jira authoring voice reference; do not import
Jira-specific sections or phrasing such as "Under this ticket" into a talk.

## Validation and delivery

- Render and inspect every changed slide. Check wrapping, bullet indentation,
  captions, figures, footer/page numbers, and branding. Check speaker notes and
  links survive export. A PDF does not validate speaker notes.
- Reused layouts can omit footer/slide-number placeholders; restore the actual
  template treatment and renumber after adding/reordering slides.
- Normalize list indentation and paragraph bullets explicitly; template
  inheritance may suppress bullets or create inconsistent levels.
- Keep source template and user notes unchanged. Publish revisions as new Box
  versions of the established presentation/PDF when that destination is authorized,
  preserving their links and history. Verify returned size/hash.
- Do not treat a remembered destination as approval to upload future unrelated
  decks. Discover the relevant provider and inspect permissions before calls.
- Capture further user feedback in this skill when requested, without turning
  conference-specific facts into universal presentation claims.
