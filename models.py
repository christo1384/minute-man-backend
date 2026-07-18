"""
Minute Man v5 — SQLAlchemy models (schema version 3).

Schema v3 delta over v2 (ADDITIVE — see alembic/versions/0002…py):
  * New table `templates` — builtin + uploaded meeting templates; the parsed
    TemplateSpec lives in `spec` JSON (parsed once at upload, never re-parsed
    at meeting time).
  * meetings.template_id (FK templates, nullable) — the free-text `template`
    column stays for backward compat; both are written.
  * meetings.meeting_date_parsed (Date, nullable) — unambiguous parse of the
    free-text meeting_date (dates.py rules, never guessed) so registers can
    sort/filter SQL-side.

Schema v2 delta over v1 (all ADDITIVE — see alembic/versions/0001…py for the
in-place migration of live v3 databases):

  * New tables `sites` and `people` — canonical names + `aliases` JSON.
    Free text remains the source of truth on the existing columns
    (meetings.site_name, actions.who, attendees.name); nullable FK columns
    (`meetings.site_id`, `actions.who_id`, `attendees.person_id`) sit
    alongside, matched EXACTLY (case/whitespace-insensitive) — no fuzzy
    auto-merge, ever (see matching.py).
  * meetings.archived (soft delete — the UI default; hard DELETE remains).
  * actions.closed_by (who marked it closed), actions.carried_from_meeting_id
    (provenance when a carried-over action is re-committed into a new
    meeting), actions.due_date (best-effort unambiguous parse of the free-
    text by_when, anchored to the meeting date — see dates.py; NULL = no
    date, never overdue).
  * Indexes: actions(status), actions(who), meetings(archived), all new FKs.

Design notes — why this schema is future-proof (per 02-DATABASE-DESIGN):

  * Incidents / hazards / actions / decisions live in CHILD TABLES, not JSON
    blobs on the meeting row. That makes future cross-meeting queries cheap:
    "open actions by person", "hazard trends by site", "incident history"
    are all plain SELECTs — no re-modelling needed.
  * `status` / `closed_at` on actions and `status` on hazards mean a future
    action-register / hazard-register feature needs NO migration.
  * Every table carries an `extra` JSON column (default {}): new per-row
    facts can land there immediately without a schema change, then graduate
    to real columns in a later schema version.
  * The single-row `schema_meta` table records the schema version (1 for v3).
    Every future change bumps it and ships an upgrade step — the hook for
    adopting Alembic later without pain.
  * The full transcript is stored on the meeting, so past meetings can be
    re-processed if the extraction prompts improve.
  * Likely v4 candidates (designed for, deliberately NOT built): a `sites`
    table, a `people` table (dedupe attendees/assignees), action carry-over
    between meetings, and a multi-company `tenant_id`.

Portability: only generic column types are used (Integer, String, Text,
Boolean, DateTime(timezone=True), generic JSON) so the same models run on
SQLite (default) and PostgreSQL (via DATABASE_URL) unmodified.
"""

from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Site(Base):
    """v2 — canonical site names. `meetings.site_name` free text stays the
    source of truth; this table exists so cross-meeting queries can group by
    site reliably. `aliases` records raw spellings seen that map here."""

    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)  # canonical display name
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Person(Base):
    """v2 — canonical people (dedupe of attendees/assignees). Same model as
    sites: free text stays on the rows, FKs point here on exact match."""

    __tablename__ = "people"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)  # canonical
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    role: Mapped[str | None] = mapped_column(String(80), nullable=True)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Template(Base):
    """v3 — meeting templates: the two builtins plus user uploads. `spec` is
    the parsed TemplateSpec (02-TEMPLATE-UPLOAD-SPEC) — structure only, never
    workbook data rows, never attendee names."""

    __tablename__ = "templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(20), nullable=False)  # "builtin" | "uploaded"
    spec: Mapped[dict] = mapped_column(JSON, default=dict)
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)  # builtins carry {"builtin_key": "safety"|"general"}
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Meeting(Base):
    """One row per meeting — the parent record."""

    __tablename__ = "meetings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    template: Mapped[str] = mapped_column(String(20), nullable=False, index=True)  # "safety" | "general" (kept for back-compat)
    template_id: Mapped[int | None] = mapped_column(
        ForeignKey("templates.id"), nullable=True, index=True)  # v3: which template row drove this meeting
    meeting_date_parsed: Mapped[date | None] = mapped_column(
        Date, nullable=True, index=True)  # v3: unambiguous parse of meeting_date (dates.py)
    meeting_type: Mapped[str | None] = mapped_column(String(100))  # display label, e.g. "Toolbox Talk"
    site_name: Mapped[str | None] = mapped_column(String(200), index=True)
    site_id: Mapped[int | None] = mapped_column(
        ForeignKey("sites.id"), nullable=True, index=True)  # v2: exact-match FK alongside the free text
    archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)  # v2: soft delete
    meeting_date: Mapped[str | None] = mapped_column(String(20), index=True)  # ISO YYYY-MM-DD (string keeps parity with the API)
    led_by: Mapped[str | None] = mapped_column(String(120))
    summary: Mapped[str | None] = mapped_column(Text)  # the minutes summary paragraph(s)
    transcript: Mapped[str | None] = mapped_column(Text)  # full raw transcript — kept for audit / re-processing
    provider_used: Mapped[str | None] = mapped_column(String(30))  # which AI provider produced the extraction
    confirmed_by_leader: Mapped[bool] = mapped_column(Boolean, default=False)  # from the Confirm screen
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    app_version: Mapped[str | None] = mapped_column(String(20))  # e.g. "3.0.0"
    extra: Mapped[dict] = mapped_column(JSON, default=dict)  # future expansion (e.g. general template stores {"topics": [...]})
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    # Cascade-delete one-to-many children: deleting a meeting removes its rows
    # in every child table (verified by the v3 test suite).
    attendees: Mapped[list["Attendee"]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan", passive_deletes=True)
    incidents: Mapped[list["Incident"]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan", passive_deletes=True)
    hazards: Mapped[list["Hazard"]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan", passive_deletes=True)
    actions: Mapped[list["Action"]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan", passive_deletes=True,
        foreign_keys="Action.meeting_id")  # v2: Action also FKs meetings via carried_from_meeting_id
    decisions: Mapped[list["Decision"]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan", passive_deletes=True)


class Attendee(Base):
    __tablename__ = "attendees"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[int] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    signature: Mapped[str | None] = mapped_column(Text)  # signature data / initials, as today
    role: Mapped[str | None] = mapped_column(String(80), nullable=True)  # future use (e.g. "Supervisor")
    person_id: Mapped[int | None] = mapped_column(
        ForeignKey("people.id"), nullable=True, index=True)  # v2: exact-match FK
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    meeting: Mapped[Meeting] = relationship(back_populates="attendees")


class Incident(Base):
    """Safety template only — 'incidents reviewed'."""

    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[int] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)  # what happened / what was reviewed
    severity: Mapped[str | None] = mapped_column(String(30), nullable=True)  # free text in v3, e.g. "near miss"
    outcome: Mapped[str | None] = mapped_column(Text)  # review outcome / lesson
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    meeting: Mapped[Meeting] = relationship(back_populates="incidents")


class Hazard(Base):
    """Safety template only."""

    __tablename__ = "hazards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[int] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False, index=True)
    hazard: Mapped[str] = mapped_column(Text, nullable=False)
    control: Mapped[str | None] = mapped_column(Text)
    control_tier: Mapped[str | None] = mapped_column(String(40))  # one of the six HSWA tier labels
    compliance_note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="open")  # future hazard-register tracking
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    meeting: Mapped[Meeting] = relationship(back_populates="hazards")


class Action(Base):
    """Both templates."""

    __tablename__ = "actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[int] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False, index=True)
    who: Mapped[str | None] = mapped_column(String(120), index=True)  # "Unassigned — needs an owner" allowed
    who_id: Mapped[int | None] = mapped_column(
        ForeignKey("people.id"), nullable=True, index=True)  # v2: exact-match FK
    what: Mapped[str] = mapped_column(Text, nullable=False)
    by_when: Mapped[str | None] = mapped_column(String(80))  # stated timeframe as text (matches engine output)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)  # v2: unambiguous parse of by_when (dates.py); NULL = no date
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)  # v4 register uses this
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_by: Mapped[str | None] = mapped_column(String(120), nullable=True)  # v2: who marked it closed
    carried_from_meeting_id: Mapped[int | None] = mapped_column(
        ForeignKey("meetings.id", ondelete="SET NULL"),
        nullable=True, index=True)  # v2: provenance when re-committed via carry-over; survives source deletion
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    meeting: Mapped[Meeting] = relationship(back_populates="actions", foreign_keys=[meeting_id])


class Decision(Base):
    """Both templates."""

    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    meeting_id: Mapped[int] = mapped_column(
        ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False, index=True)
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    meeting: Mapped[Meeting] = relationship(back_populates="decisions")


class SchemaMeta(Base):
    """Single row: which schema version this database is on (1 for v3)."""

    __tablename__ = "schema_meta"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
