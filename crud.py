"""
Minute Man v3 — CRUD helpers for the meetings database.

These functions do all reading/writing of meeting records so main.py stays a
thin HTTP layer. They accept/return plain dicts shaped exactly like the
/api/meetings request & response bodies (see main.py for the Pydantic models
that validate them).
"""

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from models import Action, Attendee, Decision, Hazard, Incident, Meeting


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:  # SQLite returns naive datetimes — they were stored as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


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

    meeting = Meeting(
        template=data["template"],
        meeting_type=data.get("meeting_type") or "",
        site_name=data.get("site_name") or "",
        meeting_date=data.get("meeting_date") or "",
        led_by=data.get("led_by") or "",
        summary=data.get("summary") or "",
        transcript=data.get("transcript") or "",
        provider_used=data.get("provider_used") or "",
        confirmed_by_leader=bool(data.get("confirmed_by_leader", False)),
        confirmed_at=confirmed_at,
        app_version=data.get("app_version") or "",
        extra=data.get("extra") or {},
    )
    for a in data.get("attendance", []):
        meeting.attendees.append(Attendee(
            name=a["name"], signature=a.get("signature") or "", role=a.get("role"),
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
        meeting.actions.append(Action(
            who=act.get("who") or "", what=act["what"],
            by_when=act.get("by_when") or "",
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
                  date_to: str | None = None, limit: int = 50) -> list[dict]:
    """Lightweight rows, newest first: no transcript, children as counts."""
    q = select(Meeting).options(
        selectinload(Meeting.attendees), selectinload(Meeting.incidents),
        selectinload(Meeting.hazards), selectinload(Meeting.actions),
        selectinload(Meeting.decisions),
    )
    if template:
        q = q.where(Meeting.template == template)
    if site_name:
        q = q.where(Meeting.site_name == site_name)
    if date_from:
        q = q.where(Meeting.meeting_date >= date_from)
    if date_to:
        q = q.where(Meeting.meeting_date <= date_to)
    q = q.order_by(Meeting.created_at.desc(), Meeting.id.desc()).limit(limit)
    return [_light_row(m) for m in session.execute(q).scalars().all()]


def _light_row(m: Meeting) -> dict:
    return {
        "id": m.id,
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
            {"who": a.who or "", "what": a.what, "by_when": a.by_when or "",
             "status": a.status, "extra": a.extra or {}}
            for a in m.actions
        ],
        "decisions": [d.decision for d in m.decisions],
    }


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------
def delete_meeting(session: Session, meeting_id: int) -> bool:
    """Hard delete (simple is fine for v3). Children go via cascade."""
    m = session.get(Meeting, meeting_id)
    if m is None:
        return False
    session.delete(m)
    session.commit()
    return True
