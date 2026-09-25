"""Alembic environment: sync psycopg connection, models as autogenerate target."""

from __future__ import annotations

from typing import Any

from alembic import context
from sqlalchemy import engine_from_config, pool

from tabayyun.db import Base
from tabayyun.db import models as _models  # noqa: F401  (registers every table on Base.metadata)
from tabayyun.settings import Settings

config = context.config
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", Settings().migration_url.replace("%", "%%"))

target_metadata = Base.metadata

# Tables owned by other components live in the same database but are not ours to diff.
FOREIGN_TABLE_PREFIXES = ("procrastinate_", "_timescaledb")

# Session-level advisory lock key: several api replicas may start at once and each runs the
# migration; the lock serialises them and is released when the connection closes.
MIGRATION_LOCK_KEY = 7_412_022_950_001


def include_object(obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any) -> bool:
    """Keep Procrastinate's and TimescaleDB's tables out of autogenerate."""
    if type_ == "table" and name is not None and name.startswith(FOREIGN_TABLE_PREFIXES):
        return False
    return True


def run_migrations_offline() -> None:
    """Emit SQL without a database connection (`alembic upgrade head --sql`)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against the configured database, one transaction per migration."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        connection.exec_driver_sql("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
        connection.commit()  # ends the autobegun transaction; the session lock stays held
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
