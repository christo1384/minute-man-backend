"""
Minute Man — FastAPI backend.

Endpoints:
  GET  /api/health   -> liveness check
  POST /api/minutes  -> transcript in, structured minutes out (the real engine)
  POST /export/pdf   -> minutes JSON in, PDF download out   (from export_routes)
  POST /export/excel -> minutes JSON in, .xlsx download out (from export_routes)

Run locally:
  uvicorn main:app --reload --port 8080
"""

import os
import logging
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Load .env if python-dotenv is installed (optional, nice for local dev).
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass

from llm import extract_minutes
from export_routes import router as export_router, MinutesExportRequest, AttendanceEntry

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("minute-man")

app = FastAPI(title="Minute Man API", version="1.0.0")

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
logger.info("CORS allowed origins: %s", allowed_origins)

# Mount the PDF / Excel export endpoints (punch-list A2).
app.include_router(export_router)


# ---------------------------------------------------------------------------
# /api/minutes — the real transcript -> minutes engine (punch-list A1 + A3)
# ---------------------------------------------------------------------------
class MinutesRequest(BaseModel):
    transcript: str = Field(..., min_length=10, description="Raw meeting transcript text")
    meeting_type: str = "Toolbox Talk"
    site_name: Optional[str] = ""
    meeting_date: Optional[str] = ""
    led_by: Optional[str] = ""
    provider: Optional[str] = None  # "anthropic" | "openai" | "demo"; falls back to DEFAULT_PROVIDER
    attendance: List[AttendanceEntry] = []


@app.get("/api/health")
def health_check():
    return {
        "status": "ok",
        "default_provider": os.getenv("DEFAULT_PROVIDER", "anthropic"),
        "anthropic_key": bool(os.getenv("ANTHROPIC_API_KEY")),
        "openai_key": bool(os.getenv("OPENAI_API_KEY")),
    }


@app.post("/api/minutes", response_model=MinutesExportRequest)
def make_minutes(req: MinutesRequest):
    """Turn a raw transcript into structured minutes ready for export."""
    try:
        extracted = extract_minutes(req.transcript, req.provider)
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

    # Merge the model's hazards/actions/decisions with the app-supplied metadata
    # and attendance (which never comes from the model). The result matches
    # MinutesExportRequest exactly, so it can be POSTed straight to /export/*.
    return MinutesExportRequest(
        meeting_type=req.meeting_type,
        site_name=req.site_name,
        meeting_date=req.meeting_date,
        hazards=extracted.get("hazards", []),
        actions=extracted.get("actions", []),
        decisions=extracted.get("decisions", []),
        attendance=req.attendance,
    )


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
