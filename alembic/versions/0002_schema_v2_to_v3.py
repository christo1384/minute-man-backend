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
    """DEFENSIVE / IDEMPOTENT: every step checks live state first. This
    matters because a v5 instance may have BOOTED BEFORE this migration file
    existed (e.g. a multi-commit deploy where an earlier commit went live
    first): that boot's create_all() already created the `templates` table,
    and the seeder already inserted the builtin rows — a naive create_table
    here would abort the whole migration with 'relation already exists'
    (observed on the live Neon deploy of 18 Jul). Each step therefore
    inspects before acting, and the migration is safe to re-run from any
    partial state."""
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # ---- 1. new table (skip when an earlier v5 boot already created it) ----
    if "templates" not in insp.get_table_names():
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
    meeting_cols = {c["name"] for c in insp.get_columns("meetings")}
    if "template_id" not in meeting_cols:
        op.add_column("meetings", sa.Column("template_id", sa.Integer, nullable=True))
    if "meeting_date_parsed" not in meeting_cols:
        op.add_column("meetings", sa.Column("meeting_date_parsed", sa.Date, nullable=True))
    if bind.dialect.name == "postgresql":
        existing_fks = {fk.get("name") for fk in insp.get_foreign_keys("meetings")}
        if "fk_meetings_template_id" not in existing_fks:
            op.create_foreign_key("fk_meetings_template_id", "meetings", "templates",
                                  ["template_id"], ["id"])
    existing_ix = {ix["name"] for ix in insp.get_indexes("meetings")}
    if "ix_meetings_template_id" not in existing_ix:
        op.create_index("ix_meetings_template_id", "meetings", ["template_id"])
    if "ix_meetings_meeting_date_parsed" not in existing_ix:
        op.create_index("ix_meetings_meeting_date_parsed", "meetings",
                        ["meeting_date_parsed"])

    # ---- 3. seed builtin templates (typed insert — JSON-safe on Postgres;
    #          skipped for any builtin_key an earlier boot already seeded) ----
    templates_tbl = sa.table(
        "templates",
        sa.column("id", sa.Integer), sa.column("name", sa.String),
        sa.column("source_kind", sa.String), sa.column("spec", sa.JSON),
        sa.column("archived", sa.Boolean), sa.column("extra", sa.JSON),
    )
    builtin_ids = {}
    for name, key, spec in BUILTIN_TEMPLATES:
        existing = bind.execute(sa.text(
            "SELECT id FROM templates WHERE source_kind = 'builtin' AND name = :n"),
            {"n": name}).scalar()
        if existing is not None:
            builtin_ids[key] = existing
            continue
        bind.execute(templates_tbl.insert().values(
            name=name, source_kind="builtin", spec=spec,
            archived=False, extra={"builtin_key": key}))
        builtin_ids[key] = bind.execute(sa.text(
            "SELECT id FROM templates WHERE source_kind = 'builtin' AND name = :n"),
            {"n": name}).scalar_one()

    # ---- 4. backfill meetings.template_id from the free-text column --------
    for key, tid in builtin_ids.items():
        bind.execute(sa.text(
            "UPDATE meetings SET template_id = :t "
            "WHERE template = :k AND template_id IS NULL"),
            {"t": tid, "k": key})

    # ---- 5. backfill meeting_date_parsed (unambiguous only; NULLs only) ----
    for mid, mdate in bind.execute(sa.text(
            "SELECT id, meeting_date FROM meetings "
            "WHERE meeting_date_parsed IS NULL")).fetchall():
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
