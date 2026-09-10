---
name: outlook-zoom-meeting-scheduler
description: Find mutually available Outlook calendar times, apply the organizer's scheduling preferences, obtain approval, and create an Outlook invitation with Zoom. Use for end-to-end meeting scheduling through the connected Outlook and Zoom accounts; never send an invitation before explicit approval.
---

# Outlook Zoom Meeting Scheduler

Use the `bga-connections` skill and the connected Outlook and Zoom providers.

## Discover and inspect

1. List BGA connections and select the connected Outlook and Zoom entries.
2. Inspect both providers' permissions before their first calls.
3. Read `/me` from Outlook for the organizer's email, timezone, and working hours.
4. Resolve attendee names through `/me/people`; do not guess email addresses.

Unless the user specifies otherwise, use a 30-minute duration and search the next
five business days.

## Find and rank times

Read availability with Outlook `POST /me/calendar/getSchedule`, including the
organizer and every required attendee. Use an interval compatible with the
meeting duration. Download the response when `bodyTruncated` is true.

Treat required attendees as available only when every covered availability slot
is `0` (free). Never penetrate an attendee's busy, tentative, or out-of-office
time.

For this organizer, apply these defaults unless the user overrides them:

- Avoid 11:30 AM through 1:00 PM local time.
- Treat events whose subject is exactly `IC Work` as penetrable.
- Never penetrate out-of-office time.
- Prefer a candidate directly before or after another non-penetrable meeting, so
  meetings are grouped together.
- Prefer a normal working-hours candidate over an early, late, or lunch slot.

Use `scripts/rank_meeting_times.py` for deterministic ranking when a full
`getSchedule` response is available. First rank with adjacency preferred; if no
candidate exists, present the best non-adjacent option.

Do not reveal unrelated calendar subjects or details. It is acceptable to name
the adjacent organizer meeting when explaining why a proposed slot was chosen.

## Approval gate

Before any calendar or Zoom write, present one recommended slot with the exact
date, local time, timezone, duration, attendees, and Zoom approach. Wait for
explicit user approval. A request to find time is not permission to send.

If availability may have changed materially before approval arrives, recheck the
slot before writing.

## Attach Zoom

Prefer a new one-time Zoom meeting when the Zoom connection permits meeting
creation. If it is read-only and the user did not require a unique link, read
`/users/me` and use `personal_meeting_url`. Clearly report when the personal
meeting room was used.

Place the Zoom URL both in the Outlook event's HTML body as a clickable link and
in the location. Do not label the event as a native Microsoft online meeting;
Microsoft Graph's native online-meeting provider does not represent this Zoom
link.

## Create and verify the Outlook event

After approval, create the event with Outlook `POST /me/events`:

- Include the approved subject, start, end, timezone, and required attendees.
- Add a concise purpose or agenda and the Zoom link.
- Request attendee responses and allow new-time proposals.
- Use a stable `transactionId` derived from the logical meeting and approved
  start time. Reuse the exact payload and transaction ID when retrying, so a
  transient failure does not create duplicates.

If Outlook creation is disabled, stop and ask the user to enable the Outlook
create-event permission. After they ask to retry, reuse the same payload and
transaction ID.

Verify the successful response contains the intended subject, times, attendees,
and Zoom link. Report that the invitation was sent only after a successful
creation response.
