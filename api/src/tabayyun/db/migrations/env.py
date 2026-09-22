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
    config.set_main_option("sqlalchemy.url", Settings().database_url.replace("%", "%%"))

target_metadata = Base.metadata

# Tables owned by other components live in the same database but are not ours to diff.
FOREIGN_TABLE_PREFIXES = ("procrastinate_", "_timescaledb")


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
