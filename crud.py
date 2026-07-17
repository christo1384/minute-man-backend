"""
Minute Man v3 — CRUD helpers for the meetings database.

These functions do all reading/writing of meeting records so main.py stays a
thin HTTP layer. They accept/return plain dicts shaped exactly like the
/api/meetings request & response bodies (see main.py for the Pydantic models
that validate them).
"""

from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from dates import parse_due_date
from matching import is_real_person, normalize
from models import Action, Attendee, Decision, Hazard, Incident, Meeting, Person, Site


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:  # SQLite returns naive datetimes — they were stored as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


# ---------------------------------------------------------------------------
# v4: sites/people match-or-create (EXACT normalised match — never fuzzy).
# Free text stays the source of truth; these only set the sidecar FK columns.
# ---------------------------------------------------------------------------
def match_or_create_site(session: Session, raw: str | None) -> Site | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    norm = normalize(raw)
    for s in session.execute(select(Site)).scalars():
        if normalize(s.name) == norm or any(normalize(a) == norm for a in (s.aliases or [])):
            if raw != s.name and raw not in (s.aliases or []):
                s.aliases = list(s.aliases or []) + [raw]  # record the new raw spelling
            return s
    site = Site(name=raw, aliases=[], extra={})
    session.add(site)
    session.flush()
    return site


def match_or_create_person(session: Session, raw: str | None) -> Person | None:
    if not is_real_person(raw):
        return None  # "Unassigned — needs an owner" / blank never becomes a person
    raw = str(raw).strip()
    norm = normalize(raw)
    for p in session.execute(select(Person)).scalars():
        if normalize(p.name) == norm or any(normalize(a) == norm for a in (p.aliases or [])):
            if raw != p.name and raw not in (p.aliases or []):
                p.aliases = list(p.aliases or []) + [raw]
            return p
    person = Person(name=raw, aliases=[], extra={})
    session.add(person)
    session.flush()
    return person


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
def create_meeting(session: Session, data: dict) -> Meeting:
    """Persist a full meeting record (parent + all children) and return it."""
    confirmed_at = None
    if data.get("confirmed_at"):
        raw = data["confirmed_at"]
        confirmed_at = raw if isinstance(raw, datetime) else datetime.fromisoformat(
            str(raw).replace("Z", "+00:00"))
    elif data.get("confirmed_by_leader"):
        # Confirmed but no timestamp supplied — stamp it server-side now.
        confirmed_at = datetime.now(timezone.utc)

    extra = dict(data.get("extra") or {})
    # v4: carried-over action ids are recorded on the meeting for the record
    # (they are NOT duplicated as new action rows — the register tracks them).
    if data.get("carried_over"):
        extra["carried_over"] = list(data["carried_over"])

    site = match_or_create_site(session, data.get("site_name"))

    meeting = Meeting(
        template=data["template"],
        meeting_type=data.get("meeting_type") or "",
        site_name=data.get("site_name") or "",
        site_id=site.id if site else None,          # v4 sidecar FK
        archived=False,
        meeting_date=data.get("meeting_date") or "",
        led_by=data.get("led_by") or "",
        summary=data.get("summary") or "",
        transcript=data.get("transcript") or "",
        provider_used=data.get("provider_used") or "",
        confirmed_by_leader=bool(data.get("confirmed_by_leader", False)),
        confirmed_at=confirmed_at,
        app_version=data.get("app_version") or "",
        extra=extra,
    )
    for a in data.get("attendance", []):
        person = match_or_create_person(session, a["name"])
        meeting.attendees.append(Attendee(
            name=a["name"], signature=a.get("signature") or "", role=a.get("role"),
            person_id=person.id if person else None,
            extra=a.get("extra") or {}))
    for i in data.get("incidents", []):
        meeting.incidents.append(Incident(
            description=i["description"], severity=i.get("severity"),
            outcome=i.get("outcome") or "", extra=i.get("extra") or {}))
    for h in data.get("hazards", []):
        meeting.hazards.append(Hazard(
            hazard=h["hazard"], control=h.get("control") or "",
            control_tier=h.get("control_tier") or "",
            compliance_note=h.get("compliance_note") or "",
            status=h.get("status") or "open", extra=h.get("extra") or {}))
    for act in data.get("actions", []):
        person = match_or_create_person(session, act.get("who"))
        meeting.actions.append(Action(
            who=act.get("who") or "", what=act["what"],
            who_id=person.id if person else None,
            by_when=act.get("by_when") or "",
            # v4: best-effort unambiguous parse — see dates.py; NULL = no date
            due_date=parse_due_date(act.get("by_when"), data.get("meeting_date")),
            # v4: set when the leader "re-commits" a carried-over action here
            carried_from_meeting_id=act.get("carried_from_meeting_id"),
            status=act.get("status") or "open", extra=act.get("extra") or {}))
    for d in data.get("decisions", []):
        text = d if isinstance(d, str) else d.get("decision", "")
        meeting.decisions.append(Decision(decision=text,
                                          extra=(d.get("extra") if isinstance(d, dict) else None) or {}))

    session.add(meeting)
    session.commit()
    session.refresh(meeting)
    return meeting


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------
def count_meetings(session: Session) -> int:
    return session.execute(select(func.count(Meeting.id))).scalar_one()


def list_meetings(session: Session, template: str | None = None,
                  site_name: str | None = None, date_from: str | None = None,
                  date_to: str | None = None, limit: int = 50,
                  offset: int = 0, q_text: str | None = None,
                  include_archived: bool = False) -> list[dict]:
    """Lightweight rows, newest first: no transcript, children as counts.

    v4: archived meetings are EXCLUDED unless include_archived; `q_text` is a
    case-insensitive substring search over site_name + meeting_type;
    `offset` enables "Load more" pagination.
    """
    q = select(Meeting).options(
        selectinload(Meeting.attendees), selectinload(Meeting.incidents),
        selectinload(Meeting.hazards), selectinload(Meeting.actions),
        selectinload(Meeting.decisions),
    )
    if not include_archived:
        # archived is nullable-in-DDL for migration safety: treat NULL as false
        q = q.where((Meeting.archived.is_(False)) | (Meeting.archived.is_(None)))
    if template:
        q = q.where(Meeting.template == template)
    if site_name:
        q = q.where(Meeting.site_name == site_name)
    if date_from:
        q = q.where(Meeting.meeting_date >= date_from)
    if date_to:
        q = q.where(Meeting.meeting_date <= date_to)
    if q_text:
        needle = f"%{q_text.lower()}%"
        q = q.where(func.lower(Meeting.site_name).like(needle)
                    | func.lower(Meeting.meeting_type).like(needle))
    q = q.order_by(Meeting.created_at.desc(), Meeting.id.desc()).offset(offset).limit(limit)
    return [_light_row(m) for m in session.execute(q).scalars().all()]


def _light_row(m: Meeting) -> dict:
    return {
        "id": m.id,
        "archived": bool(m.archived),
        "template": m.template,
        "meeting_type": m.meeting_type,
        "site_name": m.site_name,
        "meeting_date": m.meeting_date,
        "led_by": m.led_by,
        "confirmed_by_leader": m.confirmed_by_leader,
        "created_at": _iso(m.created_at),
        "attendee_count": len(m.attendees),
        "incident_count": len(m.incidents),
        "hazard_count": len(m.hazards),
        "action_count": len(m.actions),
        "decision_count": len(m.decisions),
    }


def get_meeting(session: Session, meeting_id: int) -> Meeting | None:
    q = select(Meeting).where(Meeting.id == meeting_id).options(
        selectinload(Meeting.attendees), selectinload(Meeting.incidents),
        selectinload(Meeting.hazards), selectinload(Meeting.actions),
        selectinload(Meeting.decisions),
    )
    return session.execute(q).scalars().first()


def meeting_to_dict(m: Meeting) -> dict:
    """Full nested record — the response shape of GET /api/meetings/{id}."""
    return {
        "id": m.id,
        "archived": bool(m.archived),
        "template": m.template,
        "meeting_type": m.meeting_type,
        "site_name": m.site_name,
        "meeting_date": m.meeting_date,
        "led_by": m.led_by,
        "summary": m.summary,
        "transcript": m.transcript,
        "provider_used": m.provider_used,
        "confirmed_by_leader": m.confirmed_by_leader,
        "confirmed_at": _iso(m.confirmed_at),
        "app_version": m.app_version,
        "extra": m.extra or {},
        "created_at": _iso(m.created_at),
        "updated_at": _iso(m.updated_at),
        "attendance": [
            {"name": a.name, "signature": a.signature or "", "role": a.role,
             "extra": a.extra or {}}
            for a in m.attendees
        ],
        "incidents": [
            {"description": i.description, "severity": i.severity,
             "outcome": i.outcome or "", "extra": i.extra or {}}
            for i in m.incidents
        ],
        "hazards": [
            {"hazard": h.hazard, "control": h.control or "",
             "control_tier": h.control_tier or "",
             "compliance_note": h.compliance_note or "",
             "status": h.status, "extra": h.extra or {}}
            for h in m.hazards
        ],
        "actions": [
            {"id": a.id, "who": a.who or "", "what": a.what, "by_when": a.by_when or "",
             "due_date": a.due_date.isoformat() if a.due_date else None,
             "status": a.status, "closed_at": _iso(a.closed_at), "closed_by": a.closed_by,
             "carried_from_meeting_id": a.carried_from_meeting_id,
             "extra": a.extra or {}}
            for a in m.actions
        ],
        "decisions": [d.decision for d in m.decisions],
    }


# ---------------------------------------------------------------------------
# Delete / archive
# ---------------------------------------------------------------------------
def delete_meeting(session: Session, meeting_id: int) -> bool:
    """Hard delete (kept from v3; the v4 UI default is archive). Children go
    via cascade; actions elsewhere that reference this meeting through
    carried_from_meeting_id keep their row (FK is SET NULL)."""
    m = session.get(Meeting, meeting_id)
    if m is None:
        return False
    session.delete(m)
    session.commit()
    return True


def set_meeting_archived(session: Session, meeting_id: int, archived: bool) -> Meeting | None:
    """v4 soft delete — the UI default instead of hard DELETE."""
    m = session.get(Meeting, meeting_id)
    if m is None:
        return None
    m.archived = bool(archived)
    session.commit()
    return m


# ---------------------------------------------------------------------------
# v4 — cross-meeting Action Register
# ---------------------------------------------------------------------------
def _action_row(a: Action, m: Meeting, today: date) -> dict:
    overdue = bool(a.status == "open" and a.due_date and a.due_date < today)
    return {
        "id": a.id,
        "who": a.who or "",
        "what": a.what,
        "by_when": a.by_when or "",
        "due_date": a.due_date.isoformat() if a.due_date else None,
        "status": a.status,
        "overdue": overdue,
        "closed_at": _iso(a.closed_at),
        "closed_by": a.closed_by,
        "carried_from_meeting_id": a.carried_from_meeting_id,
        "meeting_id": m.id,
        "meeting_date": m.meeting_date,
        "meeting_type": m.meeting_type,
        "site_name": m.site_name,
        "template": m.template,
    }


def list_actions(session: Session, status: str = "open", who: str | None = None,
                 site_name: str | None = None, template: str | None = None,
                 date_from: str | None = None, date_to: str | None = None,
                 overdue: bool = False, limit: int = 100, offset: int = 0,
                 today: date | None = None) -> tuple[list[dict], int]:
    """The v4 register query. Returns (rows, total_after_filtering).

    Sort: overdue first, then due_date ascending (no-date last), then newest
    meeting first. Free-text fields (who) are matched case/whitespace-
    insensitively, and a `who` filter also matches the person's canonical
    name and aliases. Sorting/pagination happen in Python — register sizes
    are bounded (limit ≤ 500) and meeting_date is free text, so SQL ordering
    on it would be wrong anyway.
    """
    today = today or date.today()
    q = (select(Action, Meeting)
         .join(Meeting, Action.meeting_id == Meeting.id)
         .where((Meeting.archived.is_(False)) | (Meeting.archived.is_(None))))
    if status in ("open", "closed"):
        q = q.where(Action.status == status)
    if site_name:
        q = q.where(func.lower(Meeting.site_name) == site_name.strip().lower())
    if template:
        q = q.where(Meeting.template == template)
    if date_from:
        q = q.where(Meeting.meeting_date >= date_from)
    if date_to:
        q = q.where(Meeting.meeting_date <= date_to)

    rows = [_action_row(a, m, today) for a, m in session.execute(q).all()]

    if who:
        wanted = {normalize(who)}
        person = None
        norm_who = normalize(who)
        for p in session.execute(select(Person)).scalars():
            if normalize(p.name) == norm_who or any(normalize(al) == norm_who for al in (p.aliases or [])):
                person = p
                break
        if person:
            wanted.add(normalize(person.name))
            wanted.update(normalize(al) for al in (person.aliases or []))
        rows = [r for r in rows if normalize(r["who"]) in wanted]
    if overdue:
        rows = [r for r in rows if r["overdue"]]

    def sort_key(r):
        return (
            0 if r["overdue"] else 1,
            r["due_date"] or "9999-12-31",     # ISO strings sort chronologically
            -r["meeting_id"],                  # newest meeting first (id follows creation order)
        )
    rows.sort(key=sort_key)
    total = len(rows)
    return rows[offset:offset + limit], total


def get_action(session: Session, action_id: int) -> Action | None:
    return session.get(Action, action_id)


def set_action_status(session: Session, action_id: int, status: str,
                      closed_by: str | None = None) -> dict | None:
    """Close or reopen an action. Close stamps closed_at (server-side) and
    closed_by; reopen clears both."""
    a = session.get(Action, action_id)
    if a is None:
        return None
    if status == "closed":
        a.status = "closed"
        a.closed_at = datetime.now(timezone.utc)
        a.closed_by = (closed_by or "").strip() or None
    else:  # "open" — reopen
        a.status = "open"
        a.closed_at = None
        a.closed_by = None
    session.commit()
    m = session.get(Meeting, a.meeting_id)
    return _action_row(a, m, date.today())


# ---------------------------------------------------------------------------
# v4 — site history (hazards & incidents with meeting context)
# ---------------------------------------------------------------------------
def list_hazards(session: Session, site_name: str | None = None,
                 date_from: str | None = None, date_to: str | None = None,
                 limit: int = 200) -> list[dict]:
    q = (select(Hazard, Meeting)
         .join(Meeting, Hazard.meeting_id == Meeting.id)
         .where((Meeting.archived.is_(False)) | (Meeting.archived.is_(None))))
    if site_name:
        q = q.where(func.lower(Meeting.site_name) == site_name.strip().lower())
    if date_from:
        q = q.where(Meeting.meeting_date >= date_from)
    if date_to:
        q = q.where(Meeting.meeting_date <= date_to)
    q = q.order_by(Meeting.created_at.desc(), Hazard.id.desc()).limit(limit)
    return [{
        "id": h.id, "hazard": h.hazard, "control": h.control or "",
        "control_tier": h.control_tier or "", "compliance_note": h.compliance_note or "",
        "status": h.status,
        "meeting_id": m.id, "meeting_date": m.meeting_date,
        "meeting_type": m.meeting_type, "site_name": m.site_name,
    } for h, m in session.execute(q).all()]


def list_incidents(session: Session, site_name: str | None = None,
                   date_from: str | None = None, date_to: str | None = None,
                   limit: int = 200) -> list[dict]:
    q = (select(Incident, Meeting)
         .join(Meeting, Incident.meeting_id == Meeting.id)
         .where((Meeting.archived.is_(False)) | (Meeting.archived.is_(None))))
    if site_name:
        q = q.where(func.lower(Meeting.site_name) == site_name.strip().lower())
    if date_from:
        q = q.where(Meeting.meeting_date >= date_from)
    if date_to:
        q = q.where(Meeting.meeting_date <= date_to)
    q = q.order_by(Meeting.created_at.desc(), Incident.id.desc()).limit(limit)
    return [{
        "id": i.id, "description": i.description, "severity": i.severity or "not stated",
        "outcome": i.outcome or "",
        "meeting_id": m.id, "meeting_date": m.meeting_date,
        "meeting_type": m.meeting_type, "site_name": m.site_name,
    } for i, m in session.execute(q).all()]


def count_sites(session: Session) -> int:
    return session.execute(select(func.count(Site.id))).scalar_one()


def count_people(session: Session) -> int:
    return session.execute(select(func.count(Person.id))).scalar_one()
