"""Alembic environment — deliberately minimal.

Migrations are driven PROGRAMMATICALLY from db.py at app startup (Chris never
runs commands; Render deploys must self-migrate). The connection URL comes in
via config's `sqlalchemy.url`, set by db.py from the same DATABASE_URL the
app uses — SQLite and Postgres both work.
"""

from alembic import context
from sqlalchemy import create_engine

config = context.config


def run_migrations_online():
    url = config.get_main_option("sqlalchemy.url")
    connectable = config.attributes.get("connection")  # reuse app engine if given
    if connectable is not None:
        context.configure(connection=connectable, target_metadata=None,
                          render_as_batch=url.startswith("sqlite"))
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None,
                          render_as_batch=url.startswith("sqlite"))
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("Offline migrations are not used by Minute Man.")
run_migrations_online()
