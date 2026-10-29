"""Alembic environment: runs migrations online against Postgres.

The database URL comes from jhunt settings (environment variables), never from
a config file. Tests point Alembic at a scratch database by putting a URL in
`config.attributes["database_url"]`.
"""

from alembic import context
from sqlalchemy import URL, create_engine, pool

from jhunt.platform.config import get_settings

config = context.config

# No SQLAlchemy models: migrations are written by hand as explicit DDL, so
# there is no metadata to autogenerate from.
target_metadata = None


def database_url() -> URL:
    url = config.attributes.get("database_url")
    return url if url is not None else get_settings().database_url()


def run_migrations_online() -> None:
    # NullPool: a migration run opens one connection and closes it.
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        # One transaction for the whole run: Postgres DDL is transactional, so
        # a failed migration leaves the schema as it was.
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    raise SystemExit("Offline (--sql) migrations are not supported; run against a database.")
run_migrations_online()
