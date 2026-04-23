# Voice Reference

Use these examples and rules to match Ezekiel Flaton's normal Jira ticket style.

The patterns below were derived from tickets created on or before January 23, 2026, including:
- `RSPS-3941`
- `RSPS-3940`
- `RSPS-3939`
- `RSPS-3919`
- `RSPS-3566`
- `RSPS-3475`
- `RSPS-3433`

## Style Summary

- Lead with the concrete problem or task, not process framing
- Name the affected site, app, system, or hardware early when known
- Write like an operator describing what needs attention, not like a PM writing a status memo
- Keep the wording direct and specific
- Use practical impact language such as "this is creating false alerts", "this is hard to understand", or "this will hurt throughput"
- Preserve technical names exactly when they matter, such as parameter names, station names, app names, and related ticket keys
- Use links as primary evidence instead of paraphrasing everything around them
- Keep the DoD pragmatic and outcome-based
- Avoid ceremony, motivational language, and vague abstractions

## Title Patterns

Prefer titles like these:

- `Singulation application needs fixes to not block the IOLoop`
- `Determine a reasonable "expected heartbeat rate" for sing app and adjust it in parameters`
- `Disable dimension updates for B side in Pittston`
- `Add new cognex camera bracket to environment files for Pittston RPS`

Title habits:
- start with the real issue or requested action
- prefer plain wording over polished wording
- include the system, site, or component when useful
- avoid vague titles like `Investigate issue` or `Support improvements`

## Body Patterns

Typical flow:

1. Start with the observed behavior or concrete goal.
2. Explain why it matters in operational terms.
3. Link the source thread, document, dashboard, or related ticket.
4. Close with a short DoD that defines the real outcome.

Common phrasings:

- `We noticed ...`
- `Goal is to ...`
- `Under this ticket, we should ...`
- `This will allow ...`
- `Likely also want to ...`

These phrases work because they are direct and action-oriented without sounding formal.

## Example Fragments

Use the shape of these examples, not their exact content:

- `We noticed high rates of heartbeat lost IRs in Pittston despite the singulation application continuing to run.`
- `Under this ticket, we should fix those blocks so that we can keep the expected heartbeat rate as low as possible without creating false alerts.`
- `Right now, if we look at the s3 bucket, it's very hard to understand what we're looking at.`
- `Likely also want to run bootstrapping and check picking in sim.`
- `SW and UI team are aligned on a design.`

## Recommended Default Draft Shape

Use this shape unless the evidence clearly suggests a different one:

```md
### Background

<source link(s)>

<direct statement of the problem, where it is happening, and why it matters>

### Technical Details

<exact system names, parameters, logs, screenshots, related tickets, and constraints>

### Definition of Done

<short list of concrete outcomes that make the ticket complete>
```

## Guardrails

- Do not exaggerate urgency unless the evidence shows urgency
- Do not write in polished corporate language
- Do not replace exact technical wording with summaries when the exact wording matters
- Do not leave the DoD vague; it should describe what will be true when the work is done
