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

from dates import parse_due_date, parse_meeting_date
from matching import is_real_person, normalize
from models import (Action, Attachment, Attendee, Decision, FeedToken, Hazard,
                    Incident, Meeting, Person, Site, Template, Webhook)


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
        if (s.extra or {}).get("merged_into"):
            continue  # v5: merged-away rows never match — their target does
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
        if (p.extra or {}).get("merged_into"):
            continue  # v5: merged-away rows never match — their target does
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
    # v5: custom-section rows from an uploaded template live in extra too —
    # registers/carry-over/site-history read ONLY core fields, unchanged.
    if data.get("custom_sections"):
        extra["custom_sections"] = data["custom_sections"]

    site = match_or_create_site(session, data.get("site_name"))

    meeting = Meeting(
        template=data["template"],
        template_id=data.get("template_id"),                          # v5
        meeting_date_parsed=parse_meeting_date(data.get("meeting_date")),  # v5, never guessed
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
        h_extra = dict(h.get("extra") or {})
        if h.get("custom"):  # v5: custom template columns ride in row extra
            h_extra["custom"] = h["custom"]
        meeting.hazards.append(Hazard(
            hazard=h["hazard"], control=h.get("control") or "",
            control_tier=h.get("control_tier") or "",
            compliance_note=h.get("compliance_note") or "",
            status=h.get("status") or "open", extra=h_extra))
    for act in data.get("actions", []):
        person = match_or_create_person(session, act.get("who"))
        act_extra = dict(act.get("extra") or {})
        if act.get("custom"):  # v5: custom template columns ride in row extra
            act_extra["custom"] = act["custom"]
        meeting.actions.append(Action(
            who=act.get("who") or "", what=act["what"],
            who_id=person.id if person else None,
            by_when=act.get("by_when") or "",
            # v4: best-effort unambiguous parse — see dates.py; NULL = no date
            due_date=parse_due_date(act.get("by_when"), data.get("meeting_date")),
            # v4: set when the leader "re-commits" a carried-over action here
            carried_from_meeting_id=act.get("carried_from_meeting_id"),
            status=act.get("status") or "open", extra=act_extra))
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


def resolve_carried_over(session: Session, m: Meeting) -> list[dict]:
    """v5 (03-A): resolve the meeting's extra.carried_over action ids back to
    who/what rows so a stored meeting re-exports its 'Outstanding Actions
    (carried over)' section exactly like the original meeting-time export."""
    ids = (m.extra or {}).get("carried_over") or []
    if not ids:
        return []
    rows = session.execute(
        select(Action, Meeting).join(Meeting, Action.meeting_id == Meeting.id)
        .where(Action.id.in_(ids))).all()
    by_id = {a.id: (a, mm) for a, mm in rows}
    return [{"who": a.who or "", "what": a.what,
             "original_date": mm.meeting_date or "", "by_when": a.by_when or ""}
            for a, mm in (by_id[i] for i in ids if i in by_id)]


def meeting_to_dict(m: Meeting, session: Session | None = None) -> dict:
    """Full nested record — the response shape of GET /api/meetings/{id}."""
    extra = dict(m.extra or {})
    if session is not None and extra.get("carried_over"):
        # v5: resolved rows ride alongside the ids (03-A)
        extra["carried_over_resolved"] = resolve_carried_over(session, m)
    return {
        "id": m.id,
        "archived": bool(m.archived),
        "template": m.template,
        "template_id": m.template_id,  # v5
        "meeting_date_parsed": m.meeting_date_parsed.isoformat() if m.meeting_date_parsed else None,  # v5
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
        "extra": extra,
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


def _site_clause(session: Session, site_name: str):
    """v5: a site filter matches CANONICALLY — the name (or any alias, e.g. a
    previously misspelled site that was merged) resolves to the site row and
    matches on Meeting.site_id, with free-text equality as the fallback for
    meetings that never got a site_id. This is what makes a merge fix
    carry-over for old spellings automatically (03-C)."""
    needle = site_name.strip().lower()
    text_match = func.lower(Meeting.site_name) == needle
    norm = normalize(site_name)
    for s in session.execute(select(Site)).scalars():
        if (s.extra or {}).get("merged_into"):
            continue
        if normalize(s.name) == norm or any(normalize(a) == norm for a in (s.aliases or [])):
            return (Meeting.site_id == s.id) | text_match
    return text_match


def _who_norms(session: Session, who: str) -> set[str]:
    """The set of spellings a `who` filter should match, as lower(trim())
    forms (what the SQL side can compute portably). Includes the raw filter
    value plus — when it resolves to a person row via full normalisation —
    that person's canonical name and every recorded alias in BOTH their
    fully-normalised and lower/trim raw forms. Every spelling ever saved is
    recorded as canonical-or-alias at save time, so raw coverage is complete
    even though SQL can't fold internal whitespace."""
    def lt(s):
        return str(s or "").strip().lower()

    wanted = {lt(who), normalize(who)}
    norm_who = normalize(who)
    for p in session.execute(select(Person)).scalars():
        if normalize(p.name) == norm_who or any(normalize(al) == norm_who for al in (p.aliases or [])):
            wanted.add(lt(p.name))
            wanted.add(normalize(p.name))
            for al in (p.aliases or []):
                wanted.add(lt(al))
                wanted.add(normalize(al))
            break
    return wanted


def list_actions(session: Session, status: str = "open", who: str | None = None,
                 site_name: str | None = None, template: str | None = None,
                 date_from: str | None = None, date_to: str | None = None,
                 overdue: bool = False, limit: int = 100, offset: int = 0,
                 today: date | None = None) -> tuple[list[dict], int]:
    """The register query. v5 (03-B): filtering, ORDERING and pagination all
    happen SQL-side using `meetings.meeting_date_parsed`, so the register is
    no longer capped by the old fetch-then-sort-in-Python approach — any
    number of actions pages correctly.

    Sort: overdue first, then due_date ascending (no-date last), then newest
    meeting (parsed date desc, NULLs last, id desc as the tiebreak).
    Date-range params are parsed with the standard table; a parseable bound
    filters on the parsed column, an unparseable one falls back to free-text
    comparison (pre-v5 behaviour).
    """
    from sqlalchemy import case

    today = today or date.today()
    overdue_expr = ((Action.status == "open")
                    & Action.due_date.isnot(None) & (Action.due_date < today))
    q = (select(Action, Meeting)
         .join(Meeting, Action.meeting_id == Meeting.id)
         .where((Meeting.archived.is_(False)) | (Meeting.archived.is_(None))))
    if status in ("open", "closed"):
        q = q.where(Action.status == status)
    if site_name:
        q = q.where(_site_clause(session, site_name))
    if template:
        q = q.where(Meeting.template == template)
    f_parsed = parse_meeting_date(date_from) if date_from else None
    t_parsed = parse_meeting_date(date_to) if date_to else None
    if date_from:
        q = q.where(Meeting.meeting_date_parsed >= f_parsed) if f_parsed \
            else q.where(Meeting.meeting_date >= date_from)
    if date_to:
        q = q.where(Meeting.meeting_date_parsed <= t_parsed) if t_parsed \
            else q.where(Meeting.meeting_date <= date_to)
    if who:
        # lower+trim in SQL (internal-whitespace folding isn't portable SQL;
        # aliases cover real-world variants) — the wanted set itself comes
        # from the fully-normalised Python matcher.
        wanted = {w for w in _who_norms(session, who)}
        q = q.where(func.lower(func.trim(Action.who)).in_(wanted))
    if overdue:
        q = q.where(overdue_expr)

    count_q = q.with_only_columns(func.count(Action.id)).order_by(None)
    total = session.execute(count_q).scalar_one()

    q = q.order_by(
        case((overdue_expr, 0), else_=1),                      # overdue first
        case((Action.due_date.is_(None), 1), else_=0),         # dated before no-date
        Action.due_date.asc(),
        case((Meeting.meeting_date_parsed.is_(None), 1), else_=0),
        Meeting.meeting_date_parsed.desc(),
        Action.meeting_id.desc(),
        Action.id.desc(),
    ).offset(offset).limit(limit)

    rows = [_action_row(a, m, today) for a, m in session.execute(q).all()]
    return rows, total


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
        q = q.where(_site_clause(session, site_name))
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
        q = q.where(_site_clause(session, site_name))
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


# ---------------------------------------------------------------------------
# v5 — templates
# ---------------------------------------------------------------------------
def template_to_dict(t: Template, include_spec: bool = True) -> dict:
    d = {
        "id": t.id, "name": t.name, "source_kind": t.source_kind,
        "original_filename": t.original_filename,
        "archived": bool(t.archived),
        "builtin_key": (t.extra or {}).get("builtin_key"),
        "created_at": _iso(t.created_at),
    }
    if include_spec:
        d["spec"] = t.spec or {}
    return d


def list_templates(session: Session, include_archived: bool = False) -> list[dict]:
    q = select(Template)
    if not include_archived:
        q = q.where((Template.archived.is_(False)) | (Template.archived.is_(None)))
    q = q.order_by(Template.source_kind.asc(), Template.id.asc())  # builtins first
    return [template_to_dict(t) for t in session.execute(q).scalars().all()]


def get_template(session: Session, template_id: int) -> Template | None:
    return session.get(Template, template_id)


def create_template(session: Session, name: str, spec: dict,
                    original_filename: str | None) -> Template:
    t = Template(name=name, source_kind="uploaded", spec=spec,
                 original_filename=original_filename, archived=False, extra={})
    session.add(t)
    session.commit()
    session.refresh(t)
    return t


def update_template(session: Session, template_id: int, name: str | None = None,
                    archived: bool | None = None, spec: dict | None = None) -> Template | None:
    t = session.get(Template, template_id)
    if t is None:
        return None
    if t.source_kind == "builtin" and (spec is not None or name is not None):
        # builtins are the curated baseline — only archiving is allowed, and
        # even that is discouraged; renames/spec edits are for uploads.
        raise ValueError("Builtin templates can't be renamed or re-specced.")
    if name is not None:
        t.name = name
    if archived is not None:
        t.archived = bool(archived)
    if spec is not None:
        t.spec = spec
    session.commit()
    return t


def count_templates(session: Session) -> int:
    return session.execute(select(func.count(Template.id))).scalar_one()


# ---------------------------------------------------------------------------
# v5 — sites/people merge (03-C). No fuzzy matching: candidates share a
# normalised form or overlap on recorded aliases only.
# ---------------------------------------------------------------------------
def _merge_rows(session: Session, model, source, target, repoint):
    """Shared merge mechanics: union aliases (source canonical becomes an
    alias of the target), log to target.extra.merge_history, mark the source
    row merged (extra.merged_into — sites/people have no archived column and
    schema changes aren't needed for this), repoint FKs via `repoint()`."""
    aliases = list(target.aliases or [])
    for raw in [source.name] + list(source.aliases or []):
        if raw != target.name and raw not in aliases:
            aliases.append(raw)
    target.aliases = aliases
    hist = list((target.extra or {}).get("merge_history") or [])
    hist.append({"merged_id": source.id, "merged_name": source.name,
                 "at": datetime.now(timezone.utc).isoformat()})
    target.extra = dict(target.extra or {}, merge_history=hist)
    source.extra = dict(source.extra or {}, merged_into=target.id)
    repoint()
    session.commit()


def merge_sites(session: Session, source_id: int, into_id: int) -> dict | None:
    src, tgt = session.get(Site, source_id), session.get(Site, into_id)
    if not src or not tgt or src.id == tgt.id:
        return None

    def repoint():
        for m in session.execute(select(Meeting).where(Meeting.site_id == src.id)).scalars():
            m.site_id = tgt.id
    _merge_rows(session, Site, src, tgt, repoint)
    return {"merged": src.id, "into": tgt.id, "name": tgt.name, "aliases": tgt.aliases}


def merge_people(session: Session, source_id: int, into_id: int) -> dict | None:
    src, tgt = session.get(Person, source_id), session.get(Person, into_id)
    if not src or not tgt or src.id == tgt.id:
        return None

    def repoint():
        for a in session.execute(select(Action).where(Action.who_id == src.id)).scalars():
            a.who_id = tgt.id
        for at in session.execute(select(Attendee).where(Attendee.person_id == src.id)).scalars():
            at.person_id = tgt.id
    _merge_rows(session, Person, src, tgt, repoint)
    return {"merged": src.id, "into": tgt.id, "name": tgt.name, "aliases": tgt.aliases}


def _active_named_rows(session: Session, model) -> list:
    return [r for r in session.execute(select(model)).scalars()
            if not (r.extra or {}).get("merged_into")]


def list_sites(session: Session) -> list[dict]:
    return [{"id": s.id, "name": s.name, "aliases": s.aliases or []}
            for s in _active_named_rows(session, Site)]


def list_people(session: Session) -> list[dict]:
    # v5.3: + email (schema v5) so the front-end can pre-tick "send email".
    return [{"id": p.id, "name": p.name, "aliases": p.aliases or [],
             "email": getattr(p, "email", None)}
            for p in _active_named_rows(session, Person)]


def upsert_person(session: Session, name: str, email: str | None = None,
                  role: str | None = None) -> dict:
    """v5.3 — quick-add from the setup screen. Matches the canonical name or
    any alias (case/whitespace-insensitive, same rules the registers use);
    creates the person when there's no match. Email/role only ever OVERWRITE
    when explicitly provided (never cleared implicitly)."""
    from matching import normalize

    target = normalize(name)
    found = None
    for p in _active_named_rows(session, Person):
        if normalize(p.name) == target or any(normalize(a) == target
                                              for a in (p.aliases or [])):
            found = p
            break
    if found is None:
        found = Person(name=name.strip(), aliases=[], extra={})
        session.add(found)
    if email is not None and email.strip():
        found.email = email.strip()
    if role is not None and role.strip():
        found.role = role.strip()
    session.commit()
    session.refresh(found)
    return {"id": found.id, "name": found.name, "aliases": found.aliases or [],
            "email": found.email, "role": found.role}


# ---------------------------------------------------------------------------
# v5.1 — feed tokens & webhooks (the office loop)
# ---------------------------------------------------------------------------
def create_feed_token(session: Session, label: str | None) -> FeedToken:
    import secrets

    t = FeedToken(token=secrets.token_urlsafe(32), label=(label or "").strip() or None,
                  revoked=False, extra={})
    session.add(t)
    session.commit()
    session.refresh(t)
    return t


def list_feed_tokens(session: Session) -> list[dict]:
    return [{"id": t.id, "label": t.label,
             "token_hint": "…" + t.token[-6:],  # never re-show the full token
             "revoked": bool(t.revoked), "created_at": _iso(t.created_at)}
            for t in session.execute(select(FeedToken)).scalars()]


def revoke_feed_token(session: Session, token_id: int) -> bool:
    t = session.get(FeedToken, token_id)
    if t is None:
        return False
    t.revoked = True
    session.commit()
    return True


def get_feed_token(session: Session, token: str) -> FeedToken | None:
    t = session.execute(select(FeedToken).where(FeedToken.token == token)).scalars().first()
    return None if (t is None or t.revoked) else t


def count_feeds(session: Session) -> int:
    return session.execute(select(func.count(FeedToken.id))
                           .where((FeedToken.revoked.is_(False)) | (FeedToken.revoked.is_(None)))).scalar_one()


def create_webhook(session: Session, url: str, events: list | None,
                   secret: str | None) -> tuple[Webhook, str]:
    import secrets as _s

    real_secret = (secret or "").strip() or _s.token_urlsafe(24)
    w = Webhook(url=url.strip(), secret=real_secret, events=list(events or []),
                active=True, extra={})
    session.add(w)
    session.commit()
    session.refresh(w)
    return w, real_secret  # the secret is returned ONCE, at creation


def webhook_to_dict(w: Webhook) -> dict:
    return {"id": w.id, "url": w.url, "events": w.events or [],
            "active": bool(w.active), "last_status": w.last_status,
            "last_fired_at": _iso(w.last_fired_at), "created_at": _iso(w.created_at)}


def list_webhooks(session: Session) -> list[dict]:
    return [webhook_to_dict(w) for w in session.execute(select(Webhook)).scalars()]


def update_webhook(session: Session, webhook_id: int, active: bool | None = None,
                   events: list | None = None, url: str | None = None) -> Webhook | None:
    w = session.get(Webhook, webhook_id)
    if w is None:
        return None
    if active is not None:
        w.active = bool(active)
    if events is not None:
        w.events = list(events)
    if url is not None:
        w.url = url.strip()
    session.commit()
    return w


def delete_webhook(session: Session, webhook_id: int) -> bool:
    w = session.get(Webhook, webhook_id)
    if w is None:
        return False
    session.delete(w)
    session.commit()
    return True


def count_webhooks(session: Session) -> int:
    return session.execute(select(func.count(Webhook.id))
                           .where((Webhook.active.is_(True)))).scalar_one()


def action_register_row(session: Session, action_id: int) -> dict | None:
    """One action in the register-row shape — the webhook payload shape."""
    a = session.get(Action, action_id)
    if a is None:
        return None
    m = session.get(Meeting, a.meeting_id)
    return _action_row(a, m, date.today())


# ---------------------------------------------------------------------------
# v6 — attachments (metadata rows; bytes live in R2, see storage.py)
# ---------------------------------------------------------------------------
def attachment_to_dict(att: Attachment, signed_url: str | None = None) -> dict:
    """Serialise an attachment row. `signed_url` (a short-lived R2 GET link)
    is generated fresh by the caller per request — never stored."""
    return {
        "id": att.id,
        "meeting_id": att.meeting_id,
        "kind": att.kind,
        "original_filename": att.original_filename,
        "content_type": att.content_type,
        "size_bytes": att.size_bytes,
        "transcript": att.transcript,
        "transcript_status": att.transcript_status,
        "uploaded_by": att.uploaded_by,
        "url": signed_url,  # None when storage isn't configured / not requested
        "extra": att.extra or {},
        "created_at": _iso(att.created_at),
    }


def create_attachment(session: Session, meeting_id: int, kind: str,
                      storage_key: str, original_filename: str | None,
                      content_type: str | None, size_bytes: int | None,
                      uploaded_by: str | None,
                      transcript_status: str | None = None) -> Attachment:
    att = Attachment(
        meeting_id=meeting_id, kind=kind, storage_key=storage_key,
        original_filename=original_filename, content_type=content_type,
        size_bytes=size_bytes, uploaded_by=(uploaded_by or "").strip() or None,
        transcript_status=transcript_status, extra={})
    session.add(att)
    session.commit()
    session.refresh(att)
    return att


def get_attachment(session: Session, attachment_id: int) -> Attachment | None:
    return session.get(Attachment, attachment_id)


def list_attachments(session: Session, meeting_id: int) -> list[Attachment]:
    q = (select(Attachment).where(Attachment.meeting_id == meeting_id)
         .order_by(Attachment.created_at.asc(), Attachment.id.asc()))
    return list(session.execute(q).scalars().all())


def set_attachment_transcript(session: Session, attachment_id: int,
                              transcript: str | None, status: str,
                              reason: str | None = None) -> Attachment | None:
    att = session.get(Attachment, attachment_id)
    if att is None:
        return None
    att.transcript = transcript
    att.transcript_status = status
    if reason:
        att.extra = dict(att.extra or {}, transcript_error=reason)
    session.commit()
    return att


def delete_attachment_row(session: Session, attachment_id: int) -> str | None:
    """Delete the DB row and return its storage_key so the caller can remove
    the R2 object. Returns None when the row doesn't exist."""
    att = session.get(Attachment, attachment_id)
    if att is None:
        return None
    key = att.storage_key
    session.delete(att)
    session.commit()
    return key


def count_attachments(session: Session) -> int:
    return session.execute(select(func.count(Attachment.id))).scalar_one()
