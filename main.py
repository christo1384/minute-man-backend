"""
Minute Man — FastAPI backend. v5.1 "Office Loop" on top of v5.0 "Templates".

v5.1 additions (standards, not stored credentials — see webhooks_out.py):
  GET    /api/feed/{token}/minuteman.ics -> subscribable ICS feed (meetings +
                                            open actions; token IS the auth)
  POST/GET/DELETE /api/feeds             -> feed-token management
  POST/GET/PATCH/DELETE /api/webhooks    -> outbound webhook management
  POST   /api/webhooks/{id}/test         -> send a signed sample payload
  Events fired: meeting.saved, action.created/closed/reopened, action.overdue
  Optional email digest via MM_SMTP_* env (off & invisible by default).
  The closing loop from the office is the EXISTING PATCH /api/actions/{id}.


v5 additions on top of v4 "Registers":
  POST   /api/templates          -> upload a template (JSON: filename +
                                    base64 content) -> parsed TemplateSpec
  GET    /api/templates          -> list (builtins + uploads)
  GET    /api/templates/{id}     -> one, with spec
  PATCH  /api/templates/{id}     -> rename / archive / correct the spec
  GET    /api/sites, /api/people -> canonical rows incl. near-duplicate groups
  POST   /api/sites/{id}/merge   -> merge into another site  ({"into_id": n})
  POST   /api/people/{id}/merge  -> merge into another person
  /api/minutes                   -> accepts template_id (uploaded templates get
                                    a dynamic prompt: curated core rules +
                                    injection-guarded template additions)
  /api/meetings                  -> stores template_id, custom row fields and
                                    custom sections; GET resolves carried-over
                                    ids to rows (extra.carried_over_resolved)
v4 endpoints (all unchanged): /api/health, /api/minutes, /api/meetings
(POST/GET/GET{id}/PATCH/DELETE), /api/actions (GET/PATCH), /api/hazards,
/api/incidents, /export/pdf|excel, /export/actions.xlsx.

Optional write protection: set env MM_API_KEY and every POST/PATCH/DELETE
under /api/ requires header X-API-Key (GETs and /api/health stay open).
Unset (the default), behaviour is identical to keyless v4.

Run locally:
  uvicorn main:app --reload --port 8080

Database: DATABASE_URL env (SQLite default). On startup db.init_db()
self-migrates any older database to the current schema in place — additive
only, existing rows untouched. See db.py / alembic/versions/.
"""

import os
import logging
from typing import List, Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

# Load .env if python-dotenv is installed (optional, nice for local dev).
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass

from llm import extract_minutes
from export_routes import router as export_router, MinutesExportRequest, AttendanceEntry
import crud
from db import get_schema_version, get_session, init_db, SessionLocal

import webhooks_out

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("minute-man")

APP_VERSION = "5.3.0"


def require_write_key(x_api_key: str | None = Header(None, alias="X-API-Key")):
    """v4 optional hardening: when env MM_API_KEY is set, every POST/PATCH/
    DELETE under /api/ needs the matching X-API-Key header. Reads (GETs) and
    /api/health stay open. When MM_API_KEY is unset (the default), this is a
    no-op and behaviour is identical to v3."""
    key = os.getenv("MM_API_KEY")
    if key and x_api_key != key:
        raise HTTPException(
            status_code=401,
            detail="This register is protected — send the access key in the "
                   "X-API-Key header.")

app = FastAPI(title="Minute Man API", version=APP_VERSION)

# CORS — which website origins are allowed to call this API from a browser.
# Controlled by the ALLOWED_ORIGINS env var (comma-separated). This keeps the
# real key-holding backend from being called by any random site.
#   - Local dev default includes the live Cloudflare front-end + localhost.
#   - Set ALLOWED_ORIGINS on the host to your real front-end origin(s), e.g.
#       ALLOWED_ORIGINS=https://minute-man.pages.dev
#   - Set ALLOWED_ORIGINS=* only if you deliberately want to allow everyone.
_default_origins = "https://minute-man.pages.dev,http://localhost:8080,http://127.0.0.1:8080"
_origins_env = os.getenv("ALLOWED_ORIGINS", _default_origins)
allowed_origins = [o.strip() for o in _origins_env.split(",") if o.strip()]

# Note: the CORS spec forbids credentials with a "*" wildcard origin (browsers
# silently reject that combination). This app never uses cookies or auth
# headers cross-origin, so credentials stay off — which also makes "*" valid.
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
logger.info("CORS allowed origins: %s", allowed_origins)

# Mount the PDF / Excel export endpoints (punch-list A2).
app.include_router(export_router)


# v5.1: lazy daily sweeps (overdue webhook events + optional email digest) —
# piggyback on API traffic because Render's free tier has no scheduler.
@app.middleware("http")
async def _lazy_sweep_middleware(request, call_next):
    if request.url.path.startswith("/api/"):
        webhooks_out.run_lazy_sweeps()  # cheap when nothing to do; never raises
    return await call_next(request)


# ---------------------------------------------------------------------------
# Database startup — create tables + stamp schema_meta (see db.py / models.py).
# ---------------------------------------------------------------------------
@app.on_event("startup")
def _startup_db():
    try:
        init_db()
        logger.info("Database ready (schema v1).")
    except Exception:  # pragma: no cover — bad DATABASE_URL etc.
        logger.exception("Database initialisation failed — /api/meetings will error, "
                         "but /api/minutes and /export/* still work.")


# ---------------------------------------------------------------------------
# /api/minutes — the transcript -> minutes engine.
# v3: template-aware. No template field (a v2.2 request) == "safety".
# ---------------------------------------------------------------------------
class MinutesRequest(BaseModel):
    transcript: str = Field(..., min_length=10, description="Raw meeting transcript text")
    meeting_type: str = "Toolbox Talk"
    site_name: Optional[str] = ""
    meeting_date: Optional[str] = ""
    led_by: Optional[str] = ""
    provider: Optional[str] = None  # "anthropic" | "gemini" | "openai" | "demo"; falls back to DEFAULT_PROVIDER
    template: Literal["safety", "general"] = "safety"  # v3; default keeps v2.2 behaviour
    template_id: Optional[int] = None  # v5: a templates-table row; uploads get a dynamic prompt
    attendance: List[AttendanceEntry] = []
    # v5.3 — paraphrasing levels (founders' rule: 40% toolbox talks, 80% >10min,
    # 100% >20min). Explicit level wins; else derived from duration_minutes;
    # both absent = exact v5.1 behaviour.
    paraphrase_level: Optional[int] = Field(None, ge=1, le=3)
    duration_minutes: Optional[float] = Field(None, ge=0)


@app.get("/api/health")
def health_check(session: Session = Depends(get_session)):
    # v3: database status + stored count. v4: schema_version + sites/people.
    try:
        meetings_stored = crud.count_meetings(session)
        sites = crud.count_sites(session)
        people = crud.count_people(session)
        templates_stored = crud.count_templates(session)
        feeds = crud.count_feeds(session)
        webhooks = crud.count_webhooks(session)
        database = "ok"
    except Exception:
        logger.exception("Health check: database error")
        meetings_stored = sites = people = templates_stored = feeds = webhooks = 0
        database = "error"
    return {
        "status": "ok",
        "version": APP_VERSION,
        "default_provider": os.getenv("DEFAULT_PROVIDER", "anthropic"),
        "anthropic_key": bool(os.getenv("ANTHROPIC_API_KEY")),
        "gemini_key": bool(os.getenv("GEMINI_API_KEY")),
        "openai_key": bool(os.getenv("OPENAI_API_KEY")),
        "database": database,
        "meetings_stored": meetings_stored,
        "schema_version": get_schema_version() if database == "ok" else None,
        "sites": sites,
        "people": people,
        "templates_stored": templates_stored,
        "feeds": feeds,
        "webhooks": webhooks,
        "email_configured": webhooks_out.email_configured(),
        "api_key_required": bool(os.getenv("MM_API_KEY")),
    }


@app.post("/api/minutes", dependencies=[Depends(require_write_key)])
def make_minutes(req: MinutesRequest):
    """Turn a raw transcript into structured minutes ready for export.

    Response shape by template:
      safety  — the v2.2 MinutesExportRequest shape (hazards/actions/decisions/
                attendance + metadata) PLUS the v3 fields: template, led_by,
                incidents, summary. Fully backward compatible.
      general — template, metadata, summary, topics, actions, decisions,
                attendance. No hazards, no incidents.
    """
    # v5: resolve template_id. Builtin rows route to the dedicated v4 paths
    # (regression-identical); uploaded rows get the dynamic spec-driven prompt.
    template_kind = req.template
    template_spec = None
    tpl_row = None
    if req.template_id is not None:
        with SessionLocal() as _s:
            tpl_row = crud.get_template(_s, req.template_id)
            if tpl_row is None:
                raise HTTPException(status_code=404,
                                    detail=f"No template with id {req.template_id}.")
            if tpl_row.archived:
                raise HTTPException(status_code=400,
                                    detail="That template is archived — unarchive it "
                                           "or pick another before generating minutes.")
            builtin_key = (tpl_row.extra or {}).get("builtin_key")
            if builtin_key in ("safety", "general"):
                template_kind = builtin_key
            else:
                template_spec = tpl_row.spec or {}
                has_haz = any(s.get("kind") == "hazards"
                              for s in template_spec.get("sections", []))
                template_kind = "safety" if has_haz else "general"

    # v5.3: resolve the paraphrase level (None = pre-v5.3 behaviour).
    from llm import resolve_paraphrase_level
    level = resolve_paraphrase_level(req.paraphrase_level, req.duration_minutes)

    try:
        extracted = extract_minutes(req.transcript, req.provider, template_kind,
                                    template_spec=template_spec,
                                    paraphrase_level=level)
    except KeyError as exc:
        # Missing API key for the chosen provider.
        raise HTTPException(
            status_code=400,
            detail=f"Missing API key for the selected provider ({exc}). "
            f"Set it in your .env, or use provider='demo' to test without a key.",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # pragma: no cover - provider/network errors
        logger.exception("Extraction failed")
        raise HTTPException(status_code=502, detail=f"Model provider error: {exc}")

    # v4: tell the caller which provider actually ran, so the front-end can
    # store the real value on the saved meeting.
    provider_used = (req.provider or os.getenv("DEFAULT_PROVIDER", "anthropic")).lower()

    # Merge the model's output with the app-supplied metadata and attendance
    # (which never comes from the model).
    common = {
        "template": template_kind,
        "meeting_type": req.meeting_type,
        "site_name": req.site_name,
        "meeting_date": req.meeting_date,
        "led_by": req.led_by,
        "summary": extracted.get("summary", ""),
        "actions": extracted.get("actions", []),
        "decisions": extracted.get("decisions", []),
        "attendance": [a.model_dump() for a in req.attendance],
    }
    if template_kind == "general":
        # General template: summary/topics/actions/decisions — deliberately NO
        # hazards or incidents keys at all.
        common["topics"] = extracted.get("topics", [])
        common["provider_used"] = provider_used  # v4
        if req.template_id is not None:
            common["template_id"] = req.template_id  # v5
            if extracted.get("custom_sections"):
                common["custom_sections"] = extracted["custom_sections"]
        if level is not None:
            common["paraphrase_level"] = level  # v5.3 echo (absent for old callers)
        return common

    # Safety (default): the v2.2 shape plus incidents + summary. Validating
    # through MinutesExportRequest keeps the engine->exporter contract exact —
    # this response can be POSTed straight to /export/pdf|excel or /api/meetings.
    payload = MinutesExportRequest(
        **common,
        template_id=req.template_id,                              # v5
        custom_sections=extracted.get("custom_sections") or {},   # v5
        incidents=extracted.get("incidents", []),
        hazards=extracted.get("hazards", []),
    )
    out = payload.model_dump() | {"provider_used": provider_used}  # v4 echo
    if level is not None:
        out["paraphrase_level"] = level  # v5.3 echo (absent for old callers)
    return out


# ---------------------------------------------------------------------------
# /api/meetings — the v3 meeting register (see 02-DATABASE-DESIGN.md).
# ---------------------------------------------------------------------------
class MeetingAttendeeIn(BaseModel):
    name: str
    signature: str = ""
    role: Optional[str] = None


class MeetingIncidentIn(BaseModel):
    description: str
    severity: Optional[str] = "not stated"
    outcome: str = ""


class MeetingHazardIn(BaseModel):
    hazard: str
    control: str = ""
    control_tier: str = ""
    compliance_note: str = ""
    custom: Optional[dict] = None  # v5: uploaded-template extra columns


class MeetingActionIn(BaseModel):
    who: str = ""
    what: str
    by_when: str = ""
    # v4: set when the leader "re-commits" a carried-over action into this
    # meeting — creates a fresh row linked back to the original meeting.
    carried_from_meeting_id: Optional[int] = None
    custom: Optional[dict] = None  # v5: uploaded-template extra columns


class MeetingSaveRequest(BaseModel):
    """POST /api/meetings body — a full, confirmed meeting record."""

    template: Literal["safety", "general"]
    meeting_type: str = ""
    site_name: str = ""
    meeting_date: str = ""          # ISO YYYY-MM-DD preferred (string, matching the API)
    led_by: str = ""
    summary: str = ""
    transcript: str = ""
    provider_used: str = ""
    confirmed_by_leader: bool = False
    confirmed_at: Optional[str] = None  # ISO timestamp; stamped server-side if confirmed & absent
    app_version: str = ""
    extra: dict = Field(default_factory=dict)  # e.g. {"topics": [...]} for general meetings
    attendance: List[MeetingAttendeeIn] = []
    incidents: List[MeetingIncidentIn] = []
    hazards: List[MeetingHazardIn] = []
    actions: List[MeetingActionIn] = []
    decisions: List[str] = []
    # v4: ids of open actions from previous meetings the leader acknowledged
    # as still outstanding — recorded in the meeting's extra JSON, never
    # duplicated as new action rows.
    carried_over: List[int] = []
    # v5: which templates-table row drove this meeting, and any custom-section
    # rows extracted for an uploaded template (stored in extra, register-exempt).
    template_id: Optional[int] = None
    custom_sections: dict = Field(default_factory=dict)


@app.post("/api/meetings", status_code=200, dependencies=[Depends(require_write_key)])
def save_meeting(req: MeetingSaveRequest, session: Session = Depends(get_session)):
    """Persist a full meeting record. Returns the saved meeting with its id."""
    try:
        meeting = crud.create_meeting(session, req.model_dump())
    except Exception as exc:
        logger.exception("Saving meeting failed")
        raise HTTPException(status_code=500, detail=f"Could not save the meeting: {exc}")
    saved = crud.get_meeting(session, meeting.id)
    result = crud.meeting_to_dict(saved, session)
    # v5.1: relay to the office (after commit; never blocks or fails the save)
    webhooks_out.fire_event("meeting.saved", {
        "id": saved.id, "template": saved.template, "meeting_type": saved.meeting_type,
        "site_name": saved.site_name, "meeting_date": saved.meeting_date,
        "led_by": saved.led_by, "summary": saved.summary,
        "action_ids": [a["id"] for a in result["actions"]],
        "hazard_count": len(result["hazards"]), "decision_count": len(result["decisions"]),
    })
    for a in result["actions"]:
        row = crud.action_register_row(session, a["id"])
        if row:
            webhooks_out.fire_event("action.created", row)
    return result


@app.get("/api/meetings")
def list_meetings(
    template: Optional[str] = Query(None, description='Filter: "safety" or "general"'),
    site_name: Optional[str] = Query(None, description="Filter: exact site name"),
    date_from: Optional[str] = Query(None, alias="from", description="Filter: meeting_date >= (YYYY-MM-DD)"),
    date_to: Optional[str] = Query(None, alias="to", description="Filter: meeting_date <= (YYYY-MM-DD)"),
    q: Optional[str] = Query(None, description="v4: substring search over site name + meeting type"),
    include_archived: bool = Query(False, description="v4: include archived (soft-deleted) meetings"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
):
    """List saved meetings, newest first — lightweight rows (no transcript,
    children as counts). v4 adds q / include_archived / offset."""
    if template and template not in ("safety", "general"):
        raise HTTPException(status_code=400,
                            detail='template filter must be "safety" or "general".')
    rows = crud.list_meetings(session, template=template, site_name=site_name,
                              date_from=date_from, date_to=date_to, limit=limit,
                              offset=offset, q_text=q, include_archived=include_archived)
    return {"meetings": rows, "count": len(rows)}


class MeetingPatch(BaseModel):
    """v4: the only PATCHable meeting field for now is `archived`."""

    archived: bool


@app.patch("/api/meetings/{meeting_id}", dependencies=[Depends(require_write_key)])
def patch_meeting(meeting_id: int, req: MeetingPatch,
                  session: Session = Depends(get_session)):
    """Archive (soft delete) / unarchive. The UI default instead of DELETE."""
    m = crud.set_meeting_archived(session, meeting_id, req.archived)
    if m is None:
        raise HTTPException(status_code=404, detail=f"No meeting with id {meeting_id}.")
    return {"id": m.id, "archived": bool(m.archived)}


@app.get("/api/meetings/{meeting_id}")
def get_meeting(meeting_id: int, session: Session = Depends(get_session)):
    """One full meeting with all children nested."""
    m = crud.get_meeting(session, meeting_id)
    if m is None:
        raise HTTPException(status_code=404, detail=f"No meeting with id {meeting_id}.")
    return crud.meeting_to_dict(m, session)  # v5: resolves carried-over ids


@app.delete("/api/meetings/{meeting_id}", dependencies=[Depends(require_write_key)])
def delete_meeting(meeting_id: int, session: Session = Depends(get_session)):
    """Hard delete (children cascade). Kept from v3 — the v4 UI default is
    archive; delete is reachable from the archived view."""
    if not crud.delete_meeting(session, meeting_id):
        raise HTTPException(status_code=404, detail=f"No meeting with id {meeting_id}.")
    return {"deleted": meeting_id}


# ---------------------------------------------------------------------------
# v4 — cross-meeting Action Register
# ---------------------------------------------------------------------------
@app.get("/api/actions")
def list_actions(
    status: str = Query("open", description='"open" (default) | "closed" | "all"'),
    who: Optional[str] = Query(None, description="free-text OR canonical person name"),
    site_name: Optional[str] = Query(None),
    template: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    overdue: bool = Query(False, description="true = only overdue open actions"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
):
    """The register: every action across every (non-archived) meeting, with
    meeting context and a computed `overdue` flag. Sort: overdue first, then
    due date (no-date last), then newest meeting."""
    if status not in ("open", "closed", "all"):
        raise HTTPException(status_code=422,
                            detail='status must be "open", "closed" or "all".')
    if template and template not in ("safety", "general"):
        raise HTTPException(status_code=400,
                            detail='template filter must be "safety" or "general".')
    rows, total = crud.list_actions(
        session, status=status, who=who, site_name=site_name, template=template,
        date_from=date_from, date_to=date_to, overdue=overdue,
        limit=limit, offset=offset)
    return {"actions": rows, "count": total}


class ActionPatch(BaseModel):
    status: str  # "closed" | "open"
    closed_by: Optional[str] = None


@app.patch("/api/actions/{action_id}", dependencies=[Depends(require_write_key)])
def patch_action(action_id: int, req: ActionPatch,
                 session: Session = Depends(get_session)):
    """Close (stamps closed_at server-side + closed_by) or reopen (clears
    both)."""
    if req.status not in ("open", "closed"):
        raise HTTPException(status_code=422,
                            detail='status must be "open" or "closed".')
    row = crud.set_action_status(session, action_id, req.status, req.closed_by)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No action with id {action_id}.")
    # v5.1: relay the close/reopen to the office
    webhooks_out.fire_event(
        "action.closed" if req.status == "closed" else "action.reopened", row)
    return row


# ---------------------------------------------------------------------------
# v4 — site history
# ---------------------------------------------------------------------------
@app.get("/api/hazards")
def list_hazards(
    site_name: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    limit: int = Query(200, ge=1, le=1000),
    session: Session = Depends(get_session),
):
    rows = crud.list_hazards(session, site_name=site_name, date_from=date_from,
                             date_to=date_to, limit=limit)
    return {"hazards": rows, "count": len(rows)}


@app.get("/api/incidents")
def list_incidents(
    site_name: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, alias="from"),
    date_to: Optional[str] = Query(None, alias="to"),
    limit: int = Query(200, ge=1, le=1000),
    session: Session = Depends(get_session),
):
    rows = crud.list_incidents(session, site_name=site_name, date_from=date_from,
                               date_to=date_to, limit=limit)
    return {"incidents": rows, "count": len(rows)}


# ---------------------------------------------------------------------------
# v5 — templates
# ---------------------------------------------------------------------------
class TemplateUpload(BaseModel):
    """JSON upload (filename + base64) — deliberately NOT multipart so no new
    dependency (python-multipart) is needed; the front-end reads the file
    with FileReader and posts it here."""

    filename: str
    content_base64: str
    name: Optional[str] = None  # display name; defaults to the filename stem


@app.post("/api/templates", dependencies=[Depends(require_write_key)])
def upload_template(req: TemplateUpload, session: Session = Depends(get_session)):
    import base64

    from template_engine import TemplateError, parse_template

    try:
        content = base64.b64decode(req.content_base64)
    except Exception:
        raise HTTPException(status_code=400, detail="The file upload was corrupted — try again.")
    try:
        spec, warnings = parse_template(req.filename, content)
    except TemplateError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    name = (req.name or "").strip() or \
        os.path.splitext(os.path.basename(req.filename))[0].replace("-", " ").replace("_", " ").strip()
    t = crud.create_template(session, name=name[:120], spec=spec,
                             original_filename=req.filename[:255])
    return {"template": crud.template_to_dict(t), "spec": spec, "warnings": warnings}


@app.get("/api/templates")
def list_templates(include_archived: bool = Query(False),
                   session: Session = Depends(get_session)):
    return {"templates": crud.list_templates(session, include_archived=include_archived)}


@app.get("/api/templates/{template_id}")
def get_template(template_id: int, session: Session = Depends(get_session)):
    t = crud.get_template(session, template_id)
    if t is None:
        raise HTTPException(status_code=404, detail=f"No template with id {template_id}.")
    return crud.template_to_dict(t)


class TemplatePatch(BaseModel):
    name: Optional[str] = None
    archived: Optional[bool] = None
    spec: Optional[dict] = None  # mapping-review corrections PATCH the stored spec


@app.patch("/api/templates/{template_id}", dependencies=[Depends(require_write_key)])
def patch_template(template_id: int, req: TemplatePatch,
                   session: Session = Depends(get_session)):
    if req.name is None and req.archived is None and req.spec is None:
        raise HTTPException(status_code=422, detail="Nothing to change.")
    try:
        t = crud.update_template(session, template_id, name=req.name,
                                 archived=req.archived, spec=req.spec)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if t is None:
        raise HTTPException(status_code=404, detail=f"No template with id {template_id}.")
    return crud.template_to_dict(t)


# ---------------------------------------------------------------------------
# v5 — sites/people lists + merge (03-C). No fuzzy matching: near-duplicate
# candidates share a normalised form or overlap on recorded aliases only.
# ---------------------------------------------------------------------------
@app.get("/api/sites")
def get_sites(session: Session = Depends(get_session)):
    return {"sites": crud.list_sites(session)}


@app.get("/api/people")
def get_people(session: Session = Depends(get_session)):
    return {"people": crud.list_people(session)}


class MergeRequest(BaseModel):
    into_id: int


@app.post("/api/sites/{site_id}/merge", dependencies=[Depends(require_write_key)])
def merge_site(site_id: int, req: MergeRequest, session: Session = Depends(get_session)):
    result = crud.merge_sites(session, site_id, req.into_id)
    if result is None:
        raise HTTPException(status_code=404,
                            detail="Unknown site id (or trying to merge a site into itself).")
    return result


@app.post("/api/people/{person_id}/merge", dependencies=[Depends(require_write_key)])
def merge_person(person_id: int, req: MergeRequest, session: Session = Depends(get_session)):
    result = crud.merge_people(session, person_id, req.into_id)
    if result is None:
        raise HTTPException(status_code=404,
                            detail="Unknown person id (or trying to merge a person into themselves).")
    return result


# ---------------------------------------------------------------------------
# v5.3 — quick-add a person on the day (founders' request: no trip to
# Settings) + email the record to selected members.
# ---------------------------------------------------------------------------
class PersonIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    email: Optional[str] = Field(None, max_length=200)
    role: Optional[str] = Field(None, max_length=80)


@app.post("/api/people", dependencies=[Depends(require_write_key)])
def add_person(req: PersonIn, session: Session = Depends(get_session)):
    """Create (or update by canonical/alias match) a person. Email and role
    only overwrite when provided. Used by the setup screen's quick-add."""
    if req.email and "@" not in req.email:
        raise HTTPException(status_code=422, detail="That email address doesn't look valid.")
    return crud.upsert_person(session, req.name, email=req.email, role=req.role)


class EmailRecordRequest(BaseModel):
    recipients: List[str] = Field(..., min_length=1, max_length=50)
    subject: Optional[str] = None
    payload: MinutesExportRequest  # same body /export/pdf takes


@app.post("/api/email-record", dependencies=[Depends(require_write_key)])
def email_record(req: EmailRecordRequest):
    """v5.3 — build the PDF record server-side and email it to the selected
    members. Uses the same MM_SMTP_* env config as the digest (no third-party
    credentials, the standing ruling). 400 with a friendly message when SMTP
    isn't configured."""
    if not webhooks_out.smtp_configured():
        raise HTTPException(
            status_code=400,
            detail="Email isn't configured on the server yet — set the MM_SMTP_* "
                   "variables (see .env.example), then try again.")
    bad = [r for r in req.recipients if "@" not in r]
    if bad:
        raise HTTPException(status_code=422,
                            detail=f"These don't look like email addresses: {', '.join(bad[:5])}")
    from export_routes import _build_pdf, _build_pdf_from_spec, _uploaded_spec_for
    spec = _uploaded_spec_for(req.payload)
    pdf_bytes = _build_pdf_from_spec(req.payload, spec) if spec else _build_pdf(req.payload)
    fname = f"minute-man-{(req.payload.meeting_type or 'meeting').lower().replace(' ', '-')}-{req.payload.meeting_date}.pdf"
    subject = req.subject or (f"{req.payload.meeting_type} record — "
                              f"{req.payload.site_name} ({req.payload.meeting_date})")
    text = (f"Attached is the {req.payload.meeting_type} record for "
            f"{req.payload.site_name} on {req.payload.meeting_date}, led by "
            f"{req.payload.led_by or '—'}.\n\nSent from Minute Man. "
            "AI-generated decision support — a responsible person has reviewed "
            "and confirmed this record before sending.")
    try:
        webhooks_out.send_email(req.recipients, subject, text,
                                attachment=(fname, pdf_bytes, "application/pdf"))
    except Exception as exc:
        logger.exception("email-record send failed")
        raise HTTPException(status_code=502,
                            detail=f"The mail server refused the send: {exc}")
    return {"sent_to": req.recipients, "attachment": fname}


# ---------------------------------------------------------------------------
# v5.1 — the ICS feed + feed-token management
# ---------------------------------------------------------------------------
from fastapi.responses import Response


@app.get("/api/feed/{token}/minuteman.ics")
def ics_feed(token: str,
             site: Optional[str] = Query(None),
             include: str = Query("both"),
             session: Session = Depends(get_session)):
    """The subscribable calendar/task feed. The unguessable token is the
    credential (calendar apps can't send headers). Revoked/unknown → plain
    404 with no information leak."""
    t = crud.get_feed_token(session, token)
    if t is None:
        raise HTTPException(status_code=404, detail="Not found.")
    if include not in ("meetings", "actions", "both"):
        include = "both"
    from ics_feed import build_feed

    body = build_feed(session, site_name=site, include=include)
    return Response(content=body, media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": 'inline; filename="minuteman.ics"',
                             "Cache-Control": "no-cache"})


class FeedCreate(BaseModel):
    label: Optional[str] = None


@app.post("/api/feeds", dependencies=[Depends(require_write_key)])
def create_feed(req: FeedCreate, session: Session = Depends(get_session)):
    t = crud.create_feed_token(session, req.label)
    # The FULL URL is returned exactly once, here.
    return {"id": t.id, "label": t.label,
            "url_path": f"/api/feed/{t.token}/minuteman.ics",
            "token": t.token}


@app.get("/api/feeds")
def get_feeds(session: Session = Depends(get_session)):
    return {"feeds": crud.list_feed_tokens(session)}


@app.delete("/api/feeds/{feed_id}", dependencies=[Depends(require_write_key)])
def revoke_feed(feed_id: int, session: Session = Depends(get_session)):
    if not crud.revoke_feed_token(session, feed_id):
        raise HTTPException(status_code=404, detail=f"No feed with id {feed_id}.")
    return {"revoked": feed_id}


# ---------------------------------------------------------------------------
# v5.1 — webhook management
# ---------------------------------------------------------------------------
class WebhookCreate(BaseModel):
    url: str
    events: Optional[List[str]] = None
    secret: Optional[str] = None


class WebhookPatch(BaseModel):
    active: Optional[bool] = None
    events: Optional[List[str]] = None
    url: Optional[str] = None


def _validate_events(events):
    if events:
        bad = [e for e in events if e not in webhooks_out.EVENT_NAMES]
        if bad:
            raise HTTPException(status_code=422,
                                detail=f"Unknown event(s) {bad}. Valid: {list(webhooks_out.EVENT_NAMES)}")


@app.post("/api/webhooks", dependencies=[Depends(require_write_key)])
def add_webhook(req: WebhookCreate, session: Session = Depends(get_session)):
    if not req.url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=422, detail="url must start with http(s)://")
    _validate_events(req.events)
    w, secret = crud.create_webhook(session, req.url, req.events, req.secret)
    return crud.webhook_to_dict(w) | {"secret": secret}  # secret shown ONCE


@app.get("/api/webhooks")
def get_webhooks(session: Session = Depends(get_session)):
    return {"webhooks": crud.list_webhooks(session)}  # secrets never re-shown


@app.patch("/api/webhooks/{webhook_id}", dependencies=[Depends(require_write_key)])
def patch_webhook(webhook_id: int, req: WebhookPatch,
                  session: Session = Depends(get_session)):
    _validate_events(req.events)
    w = crud.update_webhook(session, webhook_id, active=req.active,
                            events=req.events, url=req.url)
    if w is None:
        raise HTTPException(status_code=404, detail=f"No webhook with id {webhook_id}.")
    return crud.webhook_to_dict(w)


@app.delete("/api/webhooks/{webhook_id}", dependencies=[Depends(require_write_key)])
def remove_webhook(webhook_id: int, session: Session = Depends(get_session)):
    if not crud.delete_webhook(session, webhook_id):
        raise HTTPException(status_code=404, detail=f"No webhook with id {webhook_id}.")
    return {"deleted": webhook_id}


@app.post("/api/webhooks/{webhook_id}/test", dependencies=[Depends(require_write_key)])
def test_webhook(webhook_id: int, session: Session = Depends(get_session)):
    """Send a signed sample payload SYNCHRONOUSLY and report the outcome —
    this is the 'did I wire Power Automate right?' button."""
    import hashlib
    import hmac as _hmac
    import json as _json
    import urllib.error
    import urllib.request
    import uuid as _uuid

    from models import Webhook

    w = session.get(Webhook, webhook_id)
    if w is None:
        raise HTTPException(status_code=404, detail=f"No webhook with id {webhook_id}.")
    body = _json.dumps({"event": "test",
                        "sent_at": "",
                        "data": {"hello": "from Minute Man", "webhook_id": w.id}})
    sig = _hmac.new(w.secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    req2 = urllib.request.Request(
        w.url, data=body.encode(), method="POST",
        headers={"Content-Type": "application/json", "X-MinuteMan-Event": "test",
                 "X-MinuteMan-Delivery": str(_uuid.uuid4()),
                 "X-MinuteMan-Signature": sig})
    try:
        with urllib.request.urlopen(req2, timeout=5) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except Exception as exc:
        return {"delivered": False, "error": str(exc)[:160]}
    return {"delivered": 200 <= status < 300, "status": status}


# ---------------------------------------------------------------------------
# Serve the bundled front-end. Preferred layout is a flat ./index.html sitting
# next to this file (easiest to upload to GitHub in one go); we also fall back
# to a ./static folder if that's how it was deployed. Either way it's optional —
# the API works without any front-end present.
# ---------------------------------------------------------------------------
from fastapi.responses import FileResponse

_ROOT_INDEX = os.path.join(os.path.dirname(__file__), "index.html")
if os.path.exists(_ROOT_INDEX):
    @app.get("/", include_in_schema=False)
    def _serve_index():
        return FileResponse(_ROOT_INDEX)

FRONTEND_DIST = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(FRONTEND_DIST):
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="static")
