# Office Loop — Google Workspace (Calendar, Tasks) via Zapier or Make

**Minute Man never sees your Google password** — the calendar subscribes to a link, and tasks are created by a Zapier/Make automation running under YOUR Google login on their platform.

Your Minute Man address in these steps: `https://minute-man-api.onrender.com`

---

## Piece 1 — Meetings & action due-dates in Google Calendar (5 minutes, no Zapier)

1. Minute Man: **⚙ Crew & Settings → 🏢 Connect the office… → + Create feed link → 📋 Copy link**.
2. Google Calendar (on a computer): left sidebar → **Other calendars → + → From URL** → paste → **Add calendar**.
3. Done. Meetings appear on their dates. Google ignores proper "to-do" entries, so Minute Man also puts every open action WITH a due date on the calendar as an all-day **"ACTION DUE: …"** event — you lose nothing. (Google refreshes subscribed calendars every ~12–24 h; that's Google's schedule, not ours.)

## Piece 2 — Every action becomes a Google Task (Zapier, ~10 minutes)

1. zapier.com → **Create Zap** → Trigger: **Webhooks by Zapier → Catch Hook** → copy the hook URL.
2. Minute Man → **🏢 Connect the office…** → paste that URL into the webhook box → **Add** → press **Test** so Zapier catches a sample.
3. Back in Zapier: add a **Filter** step — only continue if `event` exactly matches `action.created`.
4. Action step: **Google Tasks → Create Task** — List: your team list; Title: `data what` — `data who`; Due date: `data due_date`; Notes: `mm-action-id: {{data id}}` plus site/meeting fields.
5. Turn the Zap on. (Make.com works identically: Webhooks → Custom webhook, filter, Google Tasks module.)

## Piece 3 — THE CLOSING LOOP: completing the Google Task closes it in Minute Man

> Requires the Minute Man access key (`MM_API_KEY`) to be on — the founders' post-meeting item.

1. New Zap → Trigger: **Google Tasks → Task Completed** (same list).
2. Action: **Webhooks by Zapier → Custom Request**:
   - Method `PATCH`; URL `https://minute-man-api.onrender.com/api/actions/<id>` where `<id>` is parsed from the task's Notes (`mm-action-id: …` — use Zapier's Formatter → Extract Number).
   - Headers: `Content-Type: application/json`, `X-API-Key: <your MM_API_KEY>`
   - Data: `{"status":"closed","closed_by":"Office (Google Tasks)"}`
3. On. The register and next-meeting carry-over update the moment the office ticks the task.

## Extras

- Overdue nudges in Gmail/Chat: same Catch Hook, filter `event = action.overdue`, action Gmail Send or Google Chat message.
- Weekly summary: prefer the built-in email digest (server-side `MM_SMTP_*` settings — see the generic guide) — zero Zapier tasks used.
