"""Schema v3 -> v4: office-loop plumbing (feed_tokens, webhooks, sweep state).

STRICTLY ADDITIVE AND IDEMPOTENT — the standing rule since the v5 rollout
incident: every step inspects live state before acting, so this migration is
safe whether it runs before, after, or instead of a partial v5.1 boot's
create_all(), and safe to re-run from any half-state.

  * new tables: feed_tokens, webhooks
  * schema_meta: + extra JSON (nullable) — app-level bookkeeping (daily
    lazy-sweep stamps; no background scheduler exists on Render free tier)
  * schema_meta.schema_version -> 4

No existing table's rows are touched at all.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())

    if "feed_tokens" not in tables:
        op.create_table(
            "feed_tokens",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("token", sa.String(64), nullable=False, unique=True),
            sa.Column("label", sa.String(120), nullable=True),
            sa.Column("revoked", sa.Boolean, nullable=True),
            sa.Column("extra", sa.JSON, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_feed_tokens_token", "feed_tokens", ["token"], unique=False)

    if "webhooks" not in tables:
        op.create_table(
            "webhooks",
            sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
            sa.Column("url", sa.String(500), nullable=False),
            sa.Column("secret", sa.String(64), nullable=False),
            sa.Column("events", sa.JSON, nullable=True),
            sa.Column("active", sa.Boolean, nullable=True),
            sa.Column("last_status", sa.String(120), nullable=True),
            sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("extra", sa.JSON, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        )

    meta_cols = {c["name"] for c in insp.get_columns("schema_meta")}
    if "extra" not in meta_cols:
        op.add_column("schema_meta", sa.Column("extra", sa.JSON, nullable=True))

    bind.execute(sa.text("UPDATE schema_meta SET schema_version = 4"))


def downgrade():
    raise RuntimeError(
        "Downgrade from schema v4 is deliberately unsupported — the upgrade "
        "is additive and never destroys data, so rolling back code is enough.")
