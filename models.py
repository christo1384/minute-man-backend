"""
Minute Man v3 — SQLAlchemy models (schema version 1).

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

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
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


class Meeting(Base):
    """One row per meeting — the parent record."""

    __tablename__ = "meetings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    template: Mapped[str] = mapped_column(String(20), nullable=False, index=True)  # "safety" | "general"
    meeting_type: Mapped[str | None] = mapped_column(String(100))  # display label, e.g. "Toolbox Talk"
    site_name: Mapped[str | None] = mapped_column(String(200), index=True)
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
        back_populates="meeting", cascade="all, delete-orphan", passive_deletes=True)
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
    who: Mapped[str | None] = mapped_column(String(120))  # "Unassigned — needs an owner" allowed
    what: Mapped[str] = mapped_column(Text, nullable=False)
    by_when: Mapped[str | None] = mapped_column(String(80))  # stated timeframe as text (matches engine output)
    status: Mapped[str] = mapped_column(String(20), default="open")  # future action-tracking across meetings
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # future use
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    meeting: Mapped[Meeting] = relationship(back_populates="actions")


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
