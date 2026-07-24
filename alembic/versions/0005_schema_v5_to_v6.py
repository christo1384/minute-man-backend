"""Schema v5 -> v6: attachments (R2-backed file pointers).

STRICTLY ADDITIVE AND IDEMPOTENT — same standing rule as every migration
since the v5 rollout incident: inspect live state before acting, safe to run
before/after/instead of a partial boot's create_all(), safe to re-run from
any half-state.

  * new table: attachments (child of meetings, ON DELETE CASCADE)
  * schema_meta.schema_version -> 6

No existing table's rows are touched at all. No file bytes live in the
database — attachments store only a pointer (the R2 object key) plus metadata.

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    if "attachments" not in tables:
        op.create_table(
            "attachments",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("meeting_id", sa.Integer,
                      sa.ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False),
            sa.Column("kind", sa.String(20), nullable=False),
            sa.Column("original_filename", sa.String(255), nullable=True),
            sa.Column("content_type", sa.String(120), nullable=True),
            sa.Column("size_bytes", sa.Integer, nullable=True),
            sa.Column("storage_key", sa.String(400), nullable=False),
            sa.Column("transcript", sa.Text, nullable=True),
            sa.Column("transcript_status", sa.String(20), nullable=True),
            sa.Column("uploaded_by", sa.String(120), nullable=True),
            sa.Column("extra", sa.JSON, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_attachments_meeting_id", "attachments",
                        ["meeting_id"], unique=False)

    bind.execute(sa.text("UPDATE schema_meta SET schema_version = 6"))


def downgrade():
    raise RuntimeError(
        "Downgrade from schema v6 is deliberately unsupported — the upgrade "
        "is additive and never destroys data, so rolling back code is enough.")
