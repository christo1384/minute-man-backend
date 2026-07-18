"""Schema v2 -> v3: templates table, meetings.template_id + meeting_date_parsed.

STRICTLY ADDITIVE, same discipline as 0001 — a live v4 database (SQLite or
Postgres, with real meetings) upgrades in place with zero changes to existing
rows beyond backfills that only FILL NEW COLUMNS:

  * new table: templates (builtin + uploaded meeting templates)
  * meetings: + template_id (FK templates, nullable)
              + meeting_date_parsed (Date, nullable)
  * seed: the two builtin template rows ("Safety / Toolbox Talk", "General
    Meeting") with specs from template_specs.py; existing meetings get
    template_id backfilled from their free-text template column
  * backfill: meeting_date_parsed via dates.parse_meeting_date (unambiguous
    only — ISO / "17 July 2026" / "17/07/2026"; anything else stays NULL)
  * schema_meta.schema_version -> 3

Revision ID: 0002
Revises: 0001
"""

import json
import os
import sys

import sqlalchemy as sa
from alembic import op

_APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

from dates import parse_meeting_date          # noqa: E402
from template_specs import BUILTIN_TEMPLATES  # noqa: E402

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()

    # ---- 1. new table ------------------------------------------------------
    op.create_table(
        "templates",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("source_kind", sa.String(20), nullable=False),
        sa.Column("spec", sa.JSON, nullable=True),
        sa.Column("original_filename", sa.String(255), nullable=True),
        sa.Column("archived", sa.Boolean, nullable=True),
        sa.Column("extra", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )

    # ---- 2. additive columns (same SQLite/Postgres split as 0001) ----------
    op.add_column("meetings", sa.Column("template_id", sa.Integer, nullable=True))
    op.add_column("meetings", sa.Column("meeting_date_parsed", sa.Date, nullable=True))
    if bind.dialect.name == "postgresql":
        op.create_foreign_key("fk_meetings_template_id", "meetings", "templates",
                              ["template_id"], ["id"])
    op.create_index("ix_meetings_template_id", "meetings", ["template_id"])
    op.create_index("ix_meetings_meeting_date_parsed", "meetings", ["meeting_date_parsed"])

    # ---- 3. seed builtin templates -----------------------------------------
    builtin_ids = {}
    for name, key, spec in BUILTIN_TEMPLATES:
        res = bind.execute(sa.text(
            "INSERT INTO templates (name, source_kind, spec, archived, extra) "
            "VALUES (:n, 'builtin', :s, :a, :e)"),
            {"n": name, "s": json.dumps(spec), "a": False,
             "e": json.dumps({"builtin_key": key})})
        tid = res.lastrowid if res.lastrowid else bind.execute(
            sa.text("SELECT id FROM templates WHERE name = :n"), {"n": name}).scalar_one()
        builtin_ids[key] = tid

    # ---- 4. backfill meetings.template_id from the free-text column --------
    for key, tid in builtin_ids.items():
        bind.execute(sa.text(
            "UPDATE meetings SET template_id = :t WHERE template = :k"),
            {"t": tid, "k": key})

    # ---- 5. backfill meeting_date_parsed (unambiguous only) ----------------
    for mid, mdate in bind.execute(sa.text(
            "SELECT id, meeting_date FROM meetings")).fetchall():
        parsed = parse_meeting_date(mdate)
        if parsed is not None:
            bind.execute(sa.text(
                "UPDATE meetings SET meeting_date_parsed = :d WHERE id = :i"),
                {"d": parsed, "i": mid})

    # ---- 6. stamp -----------------------------------------------------------
    bind.execute(sa.text("UPDATE schema_meta SET schema_version = 3"))


def downgrade():
    raise RuntimeError(
        "Downgrade from schema v3 is deliberately unsupported — the upgrade "
        "is additive and never destroys data, so rolling back code is enough.")
