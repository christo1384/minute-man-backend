# Office Loop — any other system (Asana, Trello, Monday, ClickUp, Slack, …)

Every major work-management tool accepts incoming webhooks (directly or via its automation platform) and can make an outgoing HTTP call. That's all Minute Man needs. **No credentials are ever stored in Minute Man.**

## 1. What Minute Man sends (webhooks)

Register a webhook (Connect the office… in the app, or `POST /api/webhooks {"url": "..."}` — key-guarded when MM_API_KEY is set). You may subscribe to specific events with `"events": ["action.created", ...]`; empty = all.

Every delivery is an HTTP POST with JSON body:

```json
{ "event": "action.created",
  "sent_at": "2026-07-19T02:15:00+00:00",
  "data": { "id": 12, "who": "Eddie M", "what": "Lay crushed metal",
            "by_when": "Today", "due_date": "2026-07-19", "status": "open",
            "overdue": false, "closed_at": null, "closed_by": null,
            "carried_from_meeting_id": null,
            "meeting_id": 3, "meeting_date": "2026-07-19",
            "meeting_type": "Toolbox Talk", "site_name": "Yard",
            "template": "safety" } }
```

Events: `meeting.saved` (light meeting + `action_ids`), `action.created`, `action.closed`, `action.reopened`, `action.overdue` (at most once per action per day, fired by a lazy daily sweep). Attendance data is never included in any payload.

Headers on every delivery:

```
X-MinuteMan-Event:     action.created
X-MinuteMan-Delivery:  <uuid, unique per attempt>
X-MinuteMan-Signature: <hex HMAC-SHA256 of the raw body, keyed with your webhook secret>
```

Verifying the signature (optional but recommended; Python example):

```python
import hmac, hashlib
def verify(raw_body: bytes, header_sig: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_sig)
```

Delivery behaviour: 5-second timeout, one retry at the next event opportunity, and failures never affect the site user. The webhook row records `last_status` so you can see health in the app. `POST /api/webhooks/{id}/test` sends a signed sample.

## 2. What the office calls back (the closing loop)

When a task is completed in your system, make one HTTP call:

```
PATCH https://minute-man-api.onrender.com/api/actions/<id>
Content-Type: application/json
X-API-Key: <MM_API_KEY>            (required once the key is switched on)

{"status": "closed", "closed_by": "<who completed it>"}
```

`<id>` is `data.id` from the original webhook — stash it in your task's notes/custom field when you create it. Reopen with `{"status":"open"}`.

## 3. The calendar feed (no automation platform at all)

`GET /api/feed/<token>/minuteman.ics` — subscribe from any calendar app. Optional `?site=<name>` and `?include=meetings|actions|both`. VEVENTs for meetings, VTODOs for open actions (+ paired "ACTION DUE" events so Google Calendar users see due dates). Tokens are created/revoked in the app; possession of the URL is the credential, so treat it like a private link.

## 4. The email digest (server-side, optional)

Set on the server (Render → Environment) and it just starts; unset and it's invisible:

```
MM_SMTP_HOST / MM_SMTP_PORT / MM_SMTP_USER / MM_SMTP_PASS / MM_SMTP_FROM
MM_DIGEST_TO   = office@example.co.nz, ops@example.co.nz
MM_DIGEST_HOUR = 6        (NZ morning, default 6)
MM_PUBLIC_URL  = https://minute-man-api.onrender.com
```

One email per day: overdue (red), due today, due this week, with a link to the register. No attendance data, ever.
