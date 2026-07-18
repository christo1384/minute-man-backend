# Office Loop — Microsoft 365 (Outlook, Planner, To Do, Teams)

Connect Minute Man to your Microsoft world in three pieces. **Minute Man never sees your Microsoft password** — everything below runs inside your own tenant using Power Automate, and the calendar simply *subscribes* to a link.

Your Minute Man address in these steps: `https://minute-man-api.onrender.com`

---

## Piece 1 — Meetings & actions in the shared Outlook calendar (5 minutes, no Power Automate)

1. In Minute Man: **⚙ Crew & Settings → 🏢 Connect the office… → + Create feed link** → **📋 Copy link**. (The link is shown once — copy it straight away. Lose it? Revoke and create a new one.)
2. In **Outlook on the web**: Calendar → **Add calendar → Subscribe from web** → paste the link → name it "Minute Man" → **Import**.
3. Done. Every meeting appears on its date, and every open action with a due date appears as a to-do (Outlook shows real tasks; overdue ones are flagged high-priority). Outlook refreshes subscribed calendars itself every few hours.

Teams: Calendar → **Add calendar** → same link. Apple: File → New Calendar Subscription.

## Piece 2 — Every action becomes a Planner task + To Do item (Power Automate, ~15 minutes)

**Build the flow:**
1. make.powerautomate.com → **Create → Automated cloud flow → skip** → search trigger **"When an HTTP request is received"** (Request connector) → create.
2. In the trigger, click **Use sample payload to generate schema** and paste:
   ```json
   {"event":"action.created","sent_at":"2026-07-19T00:00:00+00:00","data":{"id":12,"who":"Eddie M","what":"Lay crushed metal","by_when":"Today","due_date":"2026-07-19","status":"open","overdue":false,"site_name":"Yard","meeting_type":"Toolbox Talk","meeting_date":"2026-07-19","meeting_id":3,"closed_by":null,"closed_at":null}}
   ```
3. Add a **Condition**: `event` **is equal to** `action.created`. (One webhook carries all events — filter for the one you want.)
4. In the *yes* branch add **Planner → Create a task**: pick your Group and Plan; Title = `what` `—` `who` (dynamic content); Due Date = `due_date`; Bucket = per site if you keep one bucket per job.
5. (Optional) add **To Do → Add a to-do** for the assignee's list the same way.
6. Save. Copy the flow's **HTTP POST URL** from the trigger.

**Tell Minute Man about it:**
7. Minute Man → **🏢 Connect the office…** → paste the URL into the webhook box → **Add** → note the signing secret (optional to verify; see the generic guide) → press **Test** — you should see "✓ Delivered" and a run in Power Automate.

From now on every saved meeting pushes its actions into Planner within seconds. The same webhook also receives `meeting.saved`, `action.closed`, `action.reopened` and a daily `action.overdue` — add more conditions to the same flow whenever you want more behaviour (e.g. post overdue actions to a Teams channel with **Teams → Post message**).

## Piece 3 — THE CLOSING LOOP: ticking the Planner task closes it in Minute Man

> Requires the Minute Man access key (`MM_API_KEY`) to be switched on — the founders' post-meeting item. Until then, build the flow and leave it off.

1. New automated flow → trigger **Planner → When a task is completed** (pick the same Plan).
2. Add **HTTP** action:
   - Method: `PATCH`
   - URI: `https://minute-man-api.onrender.com/api/actions/@{first(split(triggerOutputs()?['body/title'],' — '))}` — **better:** when creating tasks in Piece 2, put the action id in the Planner task's **Notes** as `mm-action-id: 12` (dynamic `id`), then in this flow read it back with **Planner → Get task details** and use that id in the URI: `.../api/actions/<id from notes>`.
   - Headers: `Content-Type: application/json` and `X-API-Key: <your MM_API_KEY>`
   - Body: `{"status":"closed","closed_by":"@{triggerOutputs()?['body/completedBy/user/displayName']}"}`
3. Save. Now the site's carry-over panel and Action Register update the moment the office ticks the task — full circle.

## Troubleshooting

- Test button says "Not delivered" → the flow URL was pasted with a missing character, or the flow is turned off.
- Calendar shows nothing → Outlook can take up to a few hours on first sync; check the feed link opens in a browser (it should download `minuteman.ics`).
- Closing loop 401 → `MM_API_KEY` isn't set on the server yet, or the header name/value is wrong.
