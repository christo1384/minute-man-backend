"""
Minute Man v5.1 — outbound webhooks + lazy daily sweeps + optional email digest.

THE RULING (final): standards, not stored credentials. Deliveries are plain
signed JSON POSTs that Power Automate / Zapier / Make catch inside the
customer's own tenant — their Microsoft/Google credentials never touch us.
The closing loop back is the EXISTING PATCH /api/actions/{id} (with
MM_API_KEY once it's switched on).

Events (payload shapes frozen in the v5.1 CHANGELOG):
  meeting.saved    — light meeting row + action_ids
  action.created   — the register row shape (same as GET /api/actions rows)
  action.closed    — register row (incl. closed_by/closed_at)
  action.reopened  — register row
  action.overdue   — register row; fired AT MOST once per action per day

Delivery contract:
  POST <url> with headers
    X-MinuteMan-Event:     <event name>
    X-MinuteMan-Delivery:  <uuid4>
    X-MinuteMan-Signature: hex HMAC-SHA256 of the raw body using the
                           webhook's secret
  5s timeout. One retry at the next trigger opportunity. Failures NEVER
  block or fail the user's request: deliveries run on daemon threads after
  the DB work is committed, errors are swallowed and recorded on the
  webhook row (last_status / last_fired_at).

LAZY SWEEPS (documented decision): Render's free tier sleeps the process, so
there is no cron/scheduler. Instead, any /api request may trigger the daily
sweep once the date (NZ time) rolls over: overdue actions fire
`action.overdue` (per-action per-day stamp in the action's extra), and the
email digest (when configured) is sent once. Day stamps live in
schema_meta.extra so restarts don't double-fire.
"""

import hashlib
import hmac
import json
import logging
import os
import smtplib
import threading
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from sqlalchemy import select

logger = logging.getLogger("minute-man.webhooks")

EVENT_NAMES = ("meeting.saved", "action.created", "action.closed",
               "action.reopened", "action.overdue")

_retry_queue: list[tuple[int, str, str]] = []  # (webhook_id, event, body) — one retry each
_retry_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------
def _deliver(webhook_id: int, url: str, secret: str, event: str, body: str,
             is_retry: bool = False):
    """Runs on a daemon thread. Never raises."""
    from db import SessionLocal
    from models import Webhook

    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        url, data=body.encode(), method="POST",
        headers={
            "Content-Type": "application/json",
            "X-MinuteMan-Event": event,
            "X-MinuteMan-Delivery": str(uuid.uuid4()),
            "X-MinuteMan-Signature": sig,
        })
    status = "error"
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            status = f"{resp.status}"
    except urllib.error.HTTPError as exc:
        status = f"{exc.code}"
    except Exception as exc:  # DNS, refused, timeout…
        status = f"error: {str(exc)[:80]}"
        if not is_retry:
            with _retry_lock:
                _retry_queue.append((webhook_id, event, body))
    try:
        with SessionLocal() as session:
            wh = session.get(Webhook, webhook_id)
            if wh is not None:
                wh.last_status = f"{event} → {status}"[:120]
                wh.last_fired_at = datetime.now(timezone.utc)
                session.commit()
    except Exception:
        logger.exception("Recording webhook status failed (harmless)")


def _drain_retries():
    """One retry per failed delivery, at the next trigger opportunity."""
    from db import SessionLocal
    from models import Webhook

    with _retry_lock:
        pending, _retry_queue[:] = _retry_queue[:], []
    for webhook_id, event, body in pending:
        try:
            with SessionLocal() as session:
                wh = session.get(Webhook, webhook_id)
            if wh is not None and wh.active:
                threading.Thread(target=_deliver,
                                 args=(wh.id, wh.url, wh.secret, event, body, True),
                                 daemon=True).start()
        except Exception:
            logger.exception("Webhook retry scheduling failed (harmless)")


def fire_event(event: str, payload: dict):
    """Queue signed deliveries to every active, subscribed webhook. Call this
    AFTER the triggering transaction is committed. Never raises."""
    from db import SessionLocal
    from models import Webhook

    try:
        body = json.dumps({"event": event,
                           "sent_at": datetime.now(timezone.utc).isoformat(),
                           "data": payload})
        with SessionLocal() as session:
            hooks = [(w.id, w.url, w.secret, list(w.events or []), bool(w.active))
                     for w in session.execute(select(Webhook)).scalars()]
        for wid, url, secret, events, active in hooks:
            if not active:
                continue
            if events and event not in events:
                continue
            threading.Thread(target=_deliver, args=(wid, url, secret, event, body),
                             daemon=True).start()
        _drain_retries()
    except Exception:
        logger.exception("fire_event failed (harmless — user request unaffected)")


# ---------------------------------------------------------------------------
# Lazy daily sweeps (overdue events + email digest)
# ---------------------------------------------------------------------------
def _nz_today() -> date:
    # NZST/NZDT without external deps: UTC+12 in July (NZST). Digest timing is
    # deliberately approximate — the office reads it over coffee, not by SLA.
    return (datetime.now(timezone.utc) + timedelta(hours=12)).date()


def _get_state(session) -> dict:
    from models import SchemaMeta
    row = session.execute(select(SchemaMeta)).scalars().first()
    return dict(row.extra or {}) if row else {}


def _set_state(session, **kwargs):
    from models import SchemaMeta
    row = session.execute(select(SchemaMeta)).scalars().first()
    if row is not None:
        row.extra = dict(row.extra or {}, **kwargs)
        session.commit()


def run_lazy_sweeps():
    """Called from request middleware. Cheap when nothing to do (one small
    SELECT); never raises; never blocks meaningfully.

    MM_DISABLE_SWEEPS=1 turns the sweeps off entirely — used by the
    regression test suites to prove strict byte-identity of stored rows
    (the sweep's only write is the documented overdue bookkeeping stamp in
    actions.extra). Never set it in production."""
    from db import SessionLocal

    if os.getenv("MM_DISABLE_SWEEPS"):
        return
    try:
        today_str = _nz_today().isoformat()
        with SessionLocal() as session:
            state = _get_state(session)
            if state.get("overdue_sweep") != today_str:
                _set_state(session, overdue_sweep=today_str)  # claim first: at-most-once
                _overdue_sweep(session, today_str)
            if email_configured() and state.get("digest_sent") != today_str:
                hour_nz = (datetime.now(timezone.utc) + timedelta(hours=12)).hour
                if hour_nz >= int(os.getenv("MM_DIGEST_HOUR", "6")):
                    _set_state(session, digest_sent=today_str)
                    _send_digest(session)
    except Exception:
        logger.exception("Lazy sweep failed (harmless)")


def _overdue_sweep(session, today_str: str):
    import crud

    rows, _ = crud.list_actions(session, status="open", overdue=True, limit=500)
    from models import Action
    for r in rows:
        a = session.get(Action, r["id"])
        if a is None:
            continue
        if (a.extra or {}).get("overdue_last_notified") == today_str:
            continue  # at most once per action per day
        a.extra = dict(a.extra or {}, overdue_last_notified=today_str)
        session.commit()
        fire_event("action.overdue", r)


# ---------------------------------------------------------------------------
# Email digest (optional; invisible unless the MM_SMTP_* env vars are set)
# ---------------------------------------------------------------------------
def email_configured() -> bool:
    return bool(os.getenv("MM_SMTP_HOST") and os.getenv("MM_DIGEST_TO"))


def smtp_configured() -> bool:
    """v5.3 — the record-emailer only needs an SMTP host (recipients are
    chosen per meeting, so MM_DIGEST_TO is not required for it)."""
    return bool(os.getenv("MM_SMTP_HOST"))


def send_email(recipients: list[str], subject: str, text: str,
               html: str | None = None,
               attachment: tuple[str, bytes, str] | None = None) -> None:
    """v5.3 — shared SMTP sender (stdlib only). `attachment` is
    (filename, bytes, mimetype). Raises on failure — callers decide whether
    that's fatal (the record-emailer surfaces it; the digest swallows it)."""
    from email.mime.application import MIMEApplication

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = os.getenv("MM_SMTP_FROM", os.getenv("MM_SMTP_USER", "minuteman@localhost"))
    msg["To"] = ", ".join(recipients)
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(text, "plain"))
    if html:
        alt.attach(MIMEText(html, "html"))
    msg.attach(alt)
    if attachment:
        fname, blob, mimetype = attachment
        maintype, _, subtype = mimetype.partition("/")
        part = MIMEApplication(blob, _subtype=subtype or "octet-stream")
        part.add_header("Content-Disposition", "attachment", filename=fname)
        msg.attach(part)
    host = os.getenv("MM_SMTP_HOST")
    port = int(os.getenv("MM_SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=15) as smtp:
        if port != 25:
            try:
                smtp.starttls()
            except smtplib.SMTPNotSupportedError:
                pass
        user, pw = os.getenv("MM_SMTP_USER"), os.getenv("MM_SMTP_PASS")
        if user and pw:
            smtp.login(user, pw)
        smtp.sendmail(msg["From"], recipients, msg.as_string())


def build_digest(session) -> tuple[str, str]:
    """(plain text, simple HTML). Overdue / due today / due this week /
    carried-over counts. No attendance data, ever."""
    import crud

    today = _nz_today()
    week = today + timedelta(days=7)
    rows, _ = crud.list_actions(session, status="open", limit=500)
    overdue = [r for r in rows if r["overdue"]]
    due_today = [r for r in rows if r["due_date"] == today.isoformat()]
    due_week = [r for r in rows if r["due_date"] and not r["overdue"]
                and today.isoformat() < r["due_date"] <= week.isoformat()]
    base = os.getenv("MM_PUBLIC_URL", "https://minute-man-api.onrender.com")

    def line(r):
        return (f"- {r['what']} — {r['who'] or 'Unassigned'} "
                f"({r['site_name'] or '—'}, by {r['by_when'] or '—'})")

    text = [f"Minute Man daily digest — {today.isoformat()}", ""]
    text += [f"OVERDUE ({len(overdue)}):"] + ([line(r) for r in overdue] or ["- none"]) + [""]
    text += [f"Due today ({len(due_today)}):"] + ([line(r) for r in due_today] or ["- none"]) + [""]
    text += [f"Due this week ({len(due_week)}):"] + ([line(r) for r in due_week] or ["- none"]) + [""]
    text += [f"Open the register: {base}", ""]

    def rows_html(items, colour):
        if not items:
            return "<li>none</li>"
        return "".join(
            f'<li style="color:{colour}">{r["what"]} — <b>{r["who"] or "Unassigned"}</b> '
            f'({r["site_name"] or "—"}, by {r["by_when"] or "—"})</li>' for r in items)

    html = (f"<h3>Minute Man daily digest — {today.isoformat()}</h3>"
            f"<p><b>Overdue ({len(overdue)})</b></p><ul>{rows_html(overdue, '#c02626')}</ul>"
            f"<p><b>Due today ({len(due_today)})</b></p><ul>{rows_html(due_today, '#92400e')}</ul>"
            f"<p><b>Due this week ({len(due_week)})</b></p><ul>{rows_html(due_week, '#1e293b')}</ul>"
            f'<p><a href="{base}">Open the register</a></p>')
    return "\n".join(text), html


def _send_digest(session):
    text, html = build_digest(session)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Minute Man daily digest — {_nz_today().isoformat()}"
    msg["From"] = os.getenv("MM_SMTP_FROM", os.getenv("MM_SMTP_USER", "minuteman@localhost"))
    recipients = [x.strip() for x in os.getenv("MM_DIGEST_TO", "").split(",") if x.strip()]
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))
    host = os.getenv("MM_SMTP_HOST")
    port = int(os.getenv("MM_SMTP_PORT", "587"))
    try:
        with smtplib.SMTP(host, port, timeout=10) as smtp:
            if port != 25:
                try:
                    smtp.starttls()
                except smtplib.SMTPNotSupportedError:
                    pass
            user, pw = os.getenv("MM_SMTP_USER"), os.getenv("MM_SMTP_PASS")
            if user and pw:
                smtp.login(user, pw)
            smtp.sendmail(msg["From"], recipients, msg.as_string())
        logger.info("Digest sent to %s", recipients)
    except Exception:
        logger.exception("Digest send failed (will not retry until tomorrow)")
