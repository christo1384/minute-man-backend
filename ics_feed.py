"""
Minute Man v5.1 — the ICS calendar/task feed (hand-rolled RFC 5545, zero
dependencies).

Why ICS: Outlook, Google Calendar, Teams and Apple Calendar all SUBSCRIBE to
an ICS URL natively — no OAuth, no app registration, auto-refreshing. The
office adds the URL once and every meeting and open action appears in their
own calendar system. The unguessable feed token in the URL is the credential
(calendar apps cannot send auth headers).

Content model:
  * one VEVENT per non-archived meeting (all-day on its parsed date)
  * one VTODO per OPEN action (DUE when a due date exists; PRIORITY 1 when
    overdue); actions CLOSED within the last 7 days appear as
    STATUS:COMPLETED so office task lists tick themselves, then drop out
  * THE GOOGLE DUALITY: Google Calendar silently ignores VTODO components,
    so every OPEN action WITH a due date also gets a paired all-day VEVENT
    titled "ACTION DUE: …" (UID action-due-<id>@minute-man). Outlook/Apple
    users see proper to-dos; Google users see due-date events. Same feed,
    both worlds work.

Attendance data NEVER appears in the feed (standing rule since v1).
"""

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Action, Meeting

PRODID = "-//Minute Man//Office Loop 5.1//EN"


# ---------------------------------------------------------------------------
# RFC 5545 plumbing: escaping and 75-octet line folding
# ---------------------------------------------------------------------------
def _esc(text) -> str:
    s = str(text or "")
    s = s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    s = s.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")
    return s


def _fold(line: str) -> str:
    """RFC 5545 §3.1: lines longer than 75 octets are folded with CRLF + space.
    Folding on byte length keeps multi-byte characters intact by backing off
    to a UTF-8 boundary."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    out, chunk = [], b""
    limit = 75
    for ch in line:
        b = ch.encode("utf-8")
        if len(chunk) + len(b) > limit:
            out.append(chunk.decode("utf-8"))
            chunk = b" " + b  # continuation lines start with a space
            limit = 75
        else:
            chunk += b
    out.append(chunk.decode("utf-8"))
    return "\r\n".join(out)


def _prop(name: str, value: str) -> str:
    return _fold(f"{name}:{value}")


def _dt_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _d(d_: date) -> str:
    return d_.strftime("%Y%m%d")


# ---------------------------------------------------------------------------
# Feed builder
# ---------------------------------------------------------------------------
def build_feed(session: Session, site_name: str | None = None,
               include: str = "both", today: date | None = None) -> str:
    from crud import _site_clause  # canonical site matching, same as registers

    today = today or date.today()
    now = datetime.now(timezone.utc)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             _prop("X-WR-CALNAME", "Minute Man — meetings & actions")]

    want_meetings = include in ("meetings", "both")
    want_actions = include in ("actions", "both")

    mq = select(Meeting).where((Meeting.archived.is_(False)) | (Meeting.archived.is_(None)))
    if site_name:
        mq = mq.where(_site_clause(session, site_name))
    meetings = session.execute(mq).scalars().all()
    meetings_by_id = {m.id: m for m in meetings}

    if want_meetings:
        for m in meetings:
            if not m.meeting_date_parsed:
                continue  # free-text-only dates can't be placed on a calendar
            counts = (f"{len(m.hazards)} hazards, {len(m.actions)} actions, "
                      f"{len(m.decisions)} decisions")
            desc = (m.summary or "").strip()
            desc = (desc + ("\n" if desc else "") + counts).strip()
            lines += [
                "BEGIN:VEVENT",
                _prop("UID", f"meeting-{m.id}@minute-man"),
                _prop("DTSTAMP", _dt_utc(m.created_at or now)),
                _prop("DTSTART;VALUE=DATE", _d(m.meeting_date_parsed)),
                _prop("DTEND;VALUE=DATE", _d(m.meeting_date_parsed + timedelta(days=1))),
                _prop("SUMMARY", _esc(f"{m.meeting_type or 'Meeting'} — {m.site_name or 'site not recorded'}")),
                _prop("DESCRIPTION", _esc(desc)),
                "END:VEVENT",
            ]

    if want_actions:
        aq = (select(Action, Meeting).join(Meeting, Action.meeting_id == Meeting.id)
              .where((Meeting.archived.is_(False)) | (Meeting.archived.is_(None))))
        if site_name:
            aq = aq.where(_site_clause(session, site_name))
        recently = now - timedelta(days=7)
        for a, m in session.execute(aq).all():
            closed_recently = (a.status == "closed" and a.closed_at is not None
                               and (a.closed_at.replace(tzinfo=timezone.utc)
                                    if a.closed_at.tzinfo is None else a.closed_at) >= recently)
            if a.status != "open" and not closed_recently:
                continue
            overdue = bool(a.status == "open" and a.due_date and a.due_date < today)
            summary = f"{a.what} — {a.who or 'Unassigned'}"
            desc = (f"From {m.meeting_type or 'meeting'} at {m.site_name or '—'} "
                    f"on {m.meeting_date or '—'}. By: {a.by_when or '—'}."
                    + (" OVERDUE." if overdue else ""))
            todo = [
                "BEGIN:VTODO",
                _prop("UID", f"action-{a.id}@minute-man"),
                _prop("DTSTAMP", _dt_utc(a.created_at or now)),
                _prop("SUMMARY", _esc(summary)),
                _prop("DESCRIPTION", _esc(desc)),
            ]
            if a.due_date:
                todo.append(_prop("DUE;VALUE=DATE", _d(a.due_date)))
            if overdue:
                todo.append("PRIORITY:1")
            if a.status == "closed":
                todo.append("STATUS:COMPLETED")
                if a.closed_at:
                    todo.append(_prop("COMPLETED", _dt_utc(a.closed_at)))
            else:
                todo.append("STATUS:NEEDS-ACTION")
            todo.append("END:VTODO")
            lines += todo

            # Google duality: paired all-day VEVENT for OPEN actions with a due date
            if a.status == "open" and a.due_date:
                lines += [
                    "BEGIN:VEVENT",
                    _prop("UID", f"action-due-{a.id}@minute-man"),
                    _prop("DTSTAMP", _dt_utc(a.created_at or now)),
                    _prop("DTSTART;VALUE=DATE", _d(a.due_date)),
                    _prop("DTEND;VALUE=DATE", _d(a.due_date + timedelta(days=1))),
                    _prop("SUMMARY", _esc(f"ACTION DUE: {a.what} ({a.who or 'Unassigned'})")),
                    _prop("DESCRIPTION", _esc(desc)),
                    "END:VEVENT",
                ]

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
