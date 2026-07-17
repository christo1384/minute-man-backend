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

SCHEMA_VERSION = 1  # bump on every future schema change (see module docstring)

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


def init_db() -> None:
    """Create all tables (if missing) and stamp schema_meta with version 1.

    Safe to call on every startup: create_all() is a no-op for tables that
    already exist, and the schema_meta row is only inserted once.
    """
    from models import Base, SchemaMeta  # imported here to avoid circular import

    Base.metadata.create_all(engine)
    with SessionLocal() as session:
        existing = session.execute(select(SchemaMeta)).scalars().first()
        if existing is None:
            session.add(SchemaMeta(schema_version=SCHEMA_VERSION,
                                   applied_at=datetime.now(timezone.utc)))
            session.commit()


def get_session():
    """FastAPI dependency — one database session per request."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
