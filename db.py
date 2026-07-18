"""
Minute Man v3 — database engine & session plumbing.

Where the database lives:
  - The connection string comes from the DATABASE_URL environment variable.
  - When DATABASE_URL is NOT set, we default to a local SQLite file
    (./minuteman.db) — zero setup, works everywhere, used for all local
    testing.
  - When DATABASE_URL points at PostgreSQL (e.g. a free Neon database:
    postgresql://user:pass@host/dbname), the exact same code runs unmodified.
    That's why models.py uses only portable column types (Integer, String,
    Text, Boolean, DateTime, generic JSON) and no engine-specific SQL.

Migration strategy (v3, deliberately simple):
  - Tables are created with Base.metadata.create_all() on startup — fine
    because everything is new in v3.
  - A single-row `schema_meta` table records schema_version = 1. Every future
    schema change must bump this number and ship an upgrade step. This is the
    hook for adopting Alembic later without pain: Alembic can stamp its own
    revision table alongside, and schema_meta tells it what it's inheriting.
"""

import os
from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

SCHEMA_VERSION = 4  # v5.1 "Office Loop" — feed_tokens, webhooks, sweep bookkeeping

# sqlite:///./minuteman.db  →  a file called minuteman.db next to main.py.
DEFAULT_DB_URL = "sqlite:///./minuteman.db"
DATABASE_URL = os.getenv("DATABASE_URL", DEFAULT_DB_URL)

# Render/Heroku style URLs sometimes start "postgres://"; SQLAlchemy 2.x wants
# "postgresql://". Normalise so Chris can paste either form into the env var.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

_engine_kwargs = {}
if DATABASE_URL.startswith("sqlite"):
    # FastAPI serves requests on multiple threads; SQLite needs this flag to
    # allow the connection to hop threads safely alongside our session-per-
    # request pattern.
    _engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, **_engine_kwargs)

if DATABASE_URL.startswith("sqlite"):
    # SQLite ships with foreign-key enforcement OFF per connection. Our child
    # tables use ON DELETE CASCADE, so switch it on for every connection —
    # otherwise deleting a meeting would orphan its attendees/hazards/etc.
    # (PostgreSQL enforces foreign keys natively; no pragma needed there.)
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _set_sqlite_fk(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _alembic_config():
    """Programmatic Alembic config — no alembic.ini needed. Chris never runs
    commands: every deploy self-migrates on startup (v4 decision, see 02)."""
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location",
                        os.path.join(os.path.dirname(os.path.abspath(__file__)), "alembic"))
    cfg.set_main_option("sqlalchemy.url", DATABASE_URL)
    return cfg


def init_db() -> None:
    """Bring the database to the current schema (v2). Safe on every startup.

    Two paths:
      * BRAND-NEW database (no tables): create the current models directly
        with create_all(), stamp Alembic at head, write schema_meta = 2 —
        fresh installs never replay migration history.
      * EXISTING database: run `alembic upgrade head` programmatically. A v3
        database (schema v1, no alembic_version table) is treated as revision
        base, so the 0001 migration applies the additive v1→v2 delta —
        including backfills — without touching existing rows. An already-
        migrated database is a no-op. This is how a Render deploy upgrades
        live data with Chris doing nothing.
    """
    from sqlalchemy import inspect

    from models import Base, SchemaMeta  # imported here to avoid circular import

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    if "meetings" not in tables:
        # Fresh install — current-schema create + stamp at head.
        from alembic import command

        Base.metadata.create_all(engine)
        command.stamp(_alembic_config(), "head")
        with SessionLocal() as session:
            existing = session.execute(select(SchemaMeta)).scalars().first()
            if existing is None:
                session.add(SchemaMeta(schema_version=SCHEMA_VERSION,
                                       applied_at=datetime.now(timezone.utc)))
            else:
                existing.schema_version = SCHEMA_VERSION
            session.commit()
        _seed_builtin_templates()
        return

    # Existing database — migrate in place (no-op when already at head).
    from alembic import command

    command.upgrade(_alembic_config(), "head")
    # Belt-and-braces: create_all() also adds any table that somehow doesn't
    # exist yet (no-op otherwise) — it never alters existing tables.
    Base.metadata.create_all(engine)
    _seed_builtin_templates()  # no-op when the builtin rows already exist


def _seed_builtin_templates() -> None:
    """Insert the two builtin template rows if missing (v5). The 0002
    migration seeds them for upgraded databases; this covers fresh installs
    and is a no-op everywhere else."""
    from models import Template
    from template_specs import BUILTIN_TEMPLATES

    with SessionLocal() as session:
        existing_keys = {
            (t.extra or {}).get("builtin_key")
            for t in session.execute(select(Template).where(
                Template.source_kind == "builtin")).scalars()
        }
        changed = False
        for name, key, spec in BUILTIN_TEMPLATES:
            if key not in existing_keys:
                session.add(Template(name=name, source_kind="builtin", spec=spec,
                                     archived=False, extra={"builtin_key": key}))
                changed = True
        if changed:
            session.commit()


def get_schema_version() -> int | None:
    """Read schema_meta.schema_version (None when unreadable)."""
    from models import SchemaMeta

    try:
        with SessionLocal() as session:
            row = session.execute(select(SchemaMeta)).scalars().first()
            return row.schema_version if row else None
    except Exception:
        return None


def get_session():
    """FastAPI dependency — one database session per request."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
