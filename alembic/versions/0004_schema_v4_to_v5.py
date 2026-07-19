"""Schema v4 -> v5: people.email (for the email-the-record feature).

STRICTLY ADDITIVE AND IDEMPOTENT — the standing rule: every step inspects
live state before acting, so this migration is safe whether it runs before,
after, or instead of a partial v5.3 boot's create_all(), and safe to re-run
from any half-state.

  * people: + email (String(200), nullable) — a person's email address so a
    copy of the record can be sent to them. NEVER exposed via the ICS feed,
    webhooks, or digests (the attendance-privacy ruling extends to emails).
  * schema_meta.schema_version -> 5

No existing table's rows are touched at all.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)

    people_cols = {c["name"] for c in insp.get_columns("people")}
    if "email" not in people_cols:
        op.add_column("people", sa.Column("email", sa.String(200), nullable=True))

    bind.execute(sa.text("UPDATE schema_meta SET schema_version = 5"))


def downgrade():
    raise RuntimeError(
        "Downgrade from schema v5 is deliberately unsupported — the upgrade "
        "is additive and never destroys data, so rolling back code is enough.")
