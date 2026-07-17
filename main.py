"""
Minute Man — FastAPI backend. v3.0: two templates + a real meetings database.

Endpoints:
  GET    /api/health         -> liveness check (+ database status, v3)
  POST   /api/minutes        -> transcript in, structured minutes out (the engine)
                                v3: accepts template = "safety" (default) | "general"
  POST   /api/meetings       -> save a full confirmed meeting to the database (v3)
  GET    /api/meetings       -> list saved meetings, newest first (v3)
  GET    /api/meetings/{id}  -> one full meeting with all children (v3)
  DELETE /api/meetings/{id}  -> hard delete a meeting (v3)
  POST   /export/pdf         -> minutes JSON in, PDF download out   (from export_routes)
  POST   /export/excel       -> minutes JSON in, .xlsx download out (from export_routes)

Run locally:
  uvicorn main:app --reload --port 8080

Database:
  DATABASE_URL env var; defaults to a local SQLite file (./minuteman.db).
  See db.py for the SQLite-vs-Postgres story.
"""

import os
import logging
from typing import List, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
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
from db import get_session, init_db, SessionLocal

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("minute-man")

APP_VERSION = "3.0.0"

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
    attendance: List[AttendanceEntry] = []


@app.get("/api/health")
def health_check(session: Session = Depends(get_session)):
    # v3: also report database status + how many meetings are stored.
    try:
        meetings_stored = crud.count_meetings(session)
        database = "ok"
    except Exception:
        logger.exception("Health check: database error")
        meetings_stored = 0
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
    }


@app.post("/api/minutes")
def make_minutes(req: MinutesRequest):
    """Turn a raw transcript into structured minutes ready for export.

    Response shape by template:
      safety  — the v2.2 MinutesExportRequest shape (hazards/actions/decisions/
                attendance + metadata) PLUS the v3 fields: template, led_by,
                incidents, summary. Fully backward compatible.
      general — template, metadata, summary, topics, actions, decisions,
                attendance. No hazards, no incidents.
    """
    try:
        extracted = extract_minutes(req.transcript, req.provider, req.template)
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

    # Merge the model's output with the app-supplied metadata and attendance
    # (which never comes from the model).
    common = {
        "template": req.template,
        "meeting_type": req.meeting_type,
        "site_name": req.site_name,
        "meeting_date": req.meeting_date,
        "led_by": req.led_by,
        "summary": extracted.get("summary", ""),
        "actions": extracted.get("actions", []),
        "decisions": extracted.get("decisions", []),
        "attendance": [a.model_dump() for a in req.attendance],
    }
    if req.template == "general":
        # General template: summary/topics/actions/decisions — deliberately NO
        # hazards or incidents keys at all.
        common["topics"] = extracted.get("topics", [])
        return common

    # Safety (default): the v2.2 shape plus incidents + summary. Validating
    # through MinutesExportRequest keeps the engine->exporter contract exact —
    # this response can be POSTed straight to /export/pdf|excel or /api/meetings.
    payload = MinutesExportRequest(
        **common,
        incidents=extracted.get("incidents", []),
        hazards=extracted.get("hazards", []),
    )
    return payload.model_dump()


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


class MeetingActionIn(BaseModel):
    who: str = ""
    what: str
    by_when: str = ""


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


@app.post("/api/meetings", status_code=200)
def save_meeting(req: MeetingSaveRequest, session: Session = Depends(get_session)):
    """Persist a full meeting record. Returns the saved meeting with its id."""
    try:
        meeting = crud.create_meeting(session, req.model_dump())
    except Exception as exc:
        logger.exception("Saving meeting failed")
        raise HTTPException(status_code=500, detail=f"Could not save the meeting: {exc}")
    saved = crud.get_meeting(session, meeting.id)
    return crud.meeting_to_dict(saved)


@app.get("/api/meetings")
def list_meetings(
    template: Optional[str] = Query(None, description='Filter: "safety" or "general"'),
    site_name: Optional[str] = Query(None, description="Filter: exact site name"),
    date_from: Optional[str] = Query(None, alias="from", description="Filter: meeting_date >= (YYYY-MM-DD)"),
    date_to: Optional[str] = Query(None, alias="to", description="Filter: meeting_date <= (YYYY-MM-DD)"),
    limit: int = Query(50, ge=1, le=500),
    session: Session = Depends(get_session),
):
    """List saved meetings, newest first — lightweight rows (no transcript,
    children as counts)."""
    if template and template not in ("safety", "general"):
        raise HTTPException(status_code=400,
                            detail='template filter must be "safety" or "general".')
    rows = crud.list_meetings(session, template=template, site_name=site_name,
                              date_from=date_from, date_to=date_to, limit=limit)
    return {"meetings": rows, "count": len(rows)}


@app.get("/api/meetings/{meeting_id}")
def get_meeting(meeting_id: int, session: Session = Depends(get_session)):
    """One full meeting with all children nested."""
    m = crud.get_meeting(session, meeting_id)
    if m is None:
        raise HTTPException(status_code=404, detail=f"No meeting with id {meeting_id}.")
    return crud.meeting_to_dict(m)


@app.delete("/api/meetings/{meeting_id}")
def delete_meeting(meeting_id: int, session: Session = Depends(get_session)):
    """Hard delete (children cascade). Simple is fine for v3."""
    if not crud.delete_meeting(session, meeting_id):
        raise HTTPException(status_code=404, detail=f"No meeting with id {meeting_id}.")
    return {"deleted": meeting_id}


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
