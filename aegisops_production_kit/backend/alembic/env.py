"""Alembic migration environment.

Runs synchronously (psycopg3 sync driver) using the URL from app settings. Model
metadata is imported from app.db.models (added in M3); until then the registry is
empty and `alembic upgrade head` is a no-op.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.db.session import Base
from app.settings import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
# Alembic uses the sync psycopg3 driver; normalise a plain postgresql:// URL.
sync_url = settings.database_url
if sync_url.startswith("postgresql://"):
    sync_url = sync_url.replace("postgresql://", "postgresql+psycopg://", 1)
config.set_main_option("sqlalchemy.url", sync_url)

# Importing the models module registers all tables on Base.metadata.
try:  # models module appears in M3
    import app.db.models  # noqa: F401
except ModuleNotFoundError:
    pass

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
