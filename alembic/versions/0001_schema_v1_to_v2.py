"""Schema v1 -> v2: sites/people, soft delete, action register columns.

STRICTLY ADDITIVE — a live v3 database (SQLite or Postgres, empty or full of
real meetings) upgrades in place with zero changes to existing rows beyond
the backfills below, which only FILL NEW COLUMNS:

  * new tables: sites, people (canonical name + aliases JSON)
  * meetings:  + site_id (FK, nullable), + archived (bool, default false)
  * attendees: + person_id (FK, nullable)
  * actions:   + who_id (FK, nullable), + closed_by, + carried_from_meeting_id
               (FK meetings, nullable), + due_date (Date, nullable)
  * indexes:   actions(status), actions(who), meetings(archived), all new FKs
  * backfills: exact-match (case/whitespace-insensitive) grouping of existing
               site_name / who / attendee names creates sites/people rows and
               sets the FKs (ambiguous -> NULL, never guessed); due_date is
               parsed from by_when ONLY for unambiguous forms (dates.py)
  * schema_meta.schema_version -> 2

Revision ID: 0001
Revises: (base — a v3 database has no alembic_version table; db.py routes it
here as the first step of the single migration chain)
"""

import json
import os
import sys

import sqlalchemy as sa
from alembic import op

# the app directory (where dates.py / matching.py live) — alembic runs from
# db.py, so this is already on sys.path; belt-and-braces for manual runs:
_APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

from dates import parse_due_date          # noqa: E402
from matching import Matcher, is_real_person, normalize  # noqa: E402

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()

    # ---- 1. new tables --------------------------------------------------
    op.create_table(
        "sites",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(200), nullable=False, unique=True),
        sa.Column("aliases", sa.JSON, nullable=True),
        sa.Column("extra", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "people",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(120), nullable=False, unique=True),
        sa.Column("aliases", sa.JSON, nullable=True),
        sa.Column("role", sa.String(80), nullable=True),
        sa.Column("extra", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )

    # ---- 2. additive columns ---------------------------------------------
    # The new FK columns are added as PLAIN nullable integers: SQLite cannot
    # ALTER-in a foreign-key constraint without a full table rebuild (batch
    # copy-and-move), and "never touch existing rows" outranks a DB-level
    # constraint on nullable sidecar columns the app itself populates. On
    # PostgreSQL we add the real constraints afterwards (cheap and safe).
    # Fresh installs get the full constraints from models.py via create_all.
    op.add_column("meetings", sa.Column("site_id", sa.Integer, nullable=True))
    op.add_column("meetings", sa.Column("archived", sa.Boolean, nullable=True))
    op.add_column("attendees", sa.Column("person_id", sa.Integer, nullable=True))
    op.add_column("actions", sa.Column("who_id", sa.Integer, nullable=True))
    op.add_column("actions", sa.Column("closed_by", sa.String(120), nullable=True))
    op.add_column("actions", sa.Column("carried_from_meeting_id", sa.Integer, nullable=True))
    op.add_column("actions", sa.Column("due_date", sa.Date, nullable=True))

    if bind.dialect.name == "postgresql":
        op.create_foreign_key("fk_meetings_site_id", "meetings", "sites",
                              ["site_id"], ["id"])
        op.create_foreign_key("fk_attendees_person_id", "attendees", "people",
                              ["person_id"], ["id"])
        op.create_foreign_key("fk_actions_who_id", "actions", "people",
                              ["who_id"], ["id"])
        op.create_foreign_key("fk_actions_carried_from", "actions", "meetings",
                              ["carried_from_meeting_id"], ["id"],
                              ondelete="SET NULL")

    # default archived=false for existing rows (kept nullable-in-DDL, value set here)
    bind.execute(sa.text("UPDATE meetings SET archived = :f WHERE archived IS NULL"),
                 {"f": False})

    # ---- 3. indexes ------------------------------------------------------
    op.create_index("ix_actions_status", "actions", ["status"])
    op.create_index("ix_actions_who", "actions", ["who"])
    op.create_index("ix_meetings_archived", "meetings", ["archived"])
    op.create_index("ix_meetings_site_id", "meetings", ["site_id"])
    op.create_index("ix_attendees_person_id", "attendees", ["person_id"])
    op.create_index("ix_actions_who_id", "actions", ["who_id"])
    op.create_index("ix_actions_carried_from_meeting_id", "actions",
                    ["carried_from_meeting_id"])

    # ---- 4. backfill sites ------------------------------------------------
    # Exact-match grouping (normalize = lowercase/trim/collapse-whitespace).
    # Canonical = the first-seen raw spelling (lowest meeting id); other raw
    # variants land in aliases. Empty names are skipped (site_id stays NULL).
    sites = Matcher()
    rows = bind.execute(sa.text(
        "SELECT id, site_name FROM meetings WHERE site_name IS NOT NULL "
        "AND TRIM(site_name) != '' ORDER BY id")).fetchall()
    next_updates = []
    for mid, raw in rows:
        sid = sites.lookup(raw)
        if sid is None:
            res = bind.execute(
                sa.text("INSERT INTO sites (name, aliases, extra) "
                        "VALUES (:n, :a, :e)"),
                {"n": raw.strip(), "a": json.dumps([]), "e": json.dumps({})})
            sid = res.lastrowid if res.lastrowid else bind.execute(
                sa.text("SELECT id FROM sites WHERE name = :n"),
                {"n": raw.strip()}).scalar_one()
            sites.seed(sid, raw.strip())
        else:
            if sites.note_variant(sid, raw.strip()):
                bind.execute(sa.text("UPDATE sites SET aliases = :a WHERE id = :i"),
                             {"a": json.dumps(sites.aliases(sid)), "i": sid})
        next_updates.append({"i": mid, "s": sid})
    for u in next_updates:
        bind.execute(sa.text("UPDATE meetings SET site_id = :s WHERE id = :i"), u)

    # ---- 5. backfill people (from actions.who and attendees.name) ---------
    people = Matcher()

    def person_id_for(raw: str) -> int | None:
        if not is_real_person(raw):
            return None  # "Unassigned — needs an owner" / blank never becomes a person
        pid = people.lookup(raw)
        if pid is None:
            res = bind.execute(
                sa.text("INSERT INTO people (name, aliases, extra) "
                        "VALUES (:n, :a, :e)"),
                {"n": raw.strip(), "a": json.dumps([]), "e": json.dumps({})})
            pid = res.lastrowid if res.lastrowid else bind.execute(
                sa.text("SELECT id FROM people WHERE name = :n"),
                {"n": raw.strip()}).scalar_one()
            people.seed(pid, raw.strip())
        else:
            if people.note_variant(pid, raw.strip()):
                bind.execute(sa.text("UPDATE people SET aliases = :a WHERE id = :i"),
                             {"a": json.dumps(people.aliases(pid)), "i": pid})
        return pid

    for aid, who in bind.execute(sa.text(
            "SELECT id, who FROM actions ORDER BY id")).fetchall():
        pid = person_id_for(who or "")
        if pid is not None:
            bind.execute(sa.text("UPDATE actions SET who_id = :p WHERE id = :i"),
                         {"p": pid, "i": aid})
    for atid, name in bind.execute(sa.text(
            "SELECT id, name FROM attendees ORDER BY id")).fetchall():
        pid = person_id_for(name or "")
        if pid is not None:
            bind.execute(sa.text("UPDATE attendees SET person_id = :p WHERE id = :i"),
                         {"p": pid, "i": atid})

    # ---- 6. backfill due_date (unambiguous by_when only — see dates.py) ---
    for aid, by_when, mdate in bind.execute(sa.text(
            "SELECT a.id, a.by_when, m.meeting_date FROM actions a "
            "JOIN meetings m ON m.id = a.meeting_id")).fetchall():
        due = parse_due_date(by_when, mdate)
        if due is not None:
            bind.execute(sa.text("UPDATE actions SET due_date = :d WHERE id = :i"),
                         {"d": due, "i": aid})

    # ---- 7. stamp schema_meta --------------------------------------------
    bind.execute(sa.text("UPDATE schema_meta SET schema_version = 2"))


def downgrade():
    raise RuntimeError(
        "Downgrade from schema v2 is deliberately unsupported — the upgrade "
        "is additive and never destroys data, so rolling back code is enough.")
