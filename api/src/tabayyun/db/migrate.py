"""Programmatic Alembic entry points.

The migration scripts live inside the package (`tabayyun/db/migrations`) so that an installed
wheel, and therefore the api image, can migrate without the repository checkout. `alembic.ini`
at the api root points the CLI at the same directory for developers.

    python -m tabayyun.db.migrate upgrade [head]
    python -m tabayyun.db.migrate downgrade base
    python -m tabayyun.db.migrate current

Migrations connect as the table owner (`TABAYYUN_MIGRATION_DATABASE_URL`, falling back to
`TABAYYUN_DATABASE_URL`). After an upgrade the app login of `TABAYYUN_DATABASE_URL` is
created or updated as a member of `tabayyun_app` (spec 007, `tabayyun.db.roles`).
"""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from tabayyun.db.roles import ensure_app_login
from tabayyun.settings import Settings, get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def build_config(database_url: str, *, timescale: str | None = None) -> Config:
    """Alembic config for `database_url`; `timescale` overrides the settings value."""
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    # ConfigParser interpolation: a literal % in a password must be doubled.
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    if timescale is not None:
        cfg.attributes["timescale"] = timescale
    return cfg


def head_revision() -> str:
    """Newest revision shipped with this build."""
    script = ScriptDirectory(str(MIGRATIONS_DIR))
    heads = script.get_heads()
    if len(heads) != 1:
        raise RuntimeError(f"expected exactly one migration head, found {heads}")
    return heads[0]


def upgrade(
    database_url: str,
    revision: str = "head",
    *,
    timescale: str | None = None,
    app_database_url: str | None = None,
) -> None:
    """Apply migrations up to `revision` as the owner of `database_url`, then provision the
    login of `app_database_url` (when given and different from the owner)."""
    command.upgrade(build_config(database_url, timescale=timescale), revision)
    if app_database_url is not None:
        ensure_app_login(database_url, app_database_url)


def downgrade(database_url: str, revision: str, *, timescale: str | None = None) -> None:
    """Revert migrations down to `revision` (`base` empties the schema)."""
    command.downgrade(build_config(database_url, timescale=timescale), revision)


def check(database_url: str) -> None:
    """Raise when the models and the migrations disagree (autogenerate would not be empty)."""
    command.check(build_config(database_url))


def main(argv: list[str] | None = None) -> int:
    """Tiny CLI used by the container entrypoint; settings supply the database URL."""
    args = argv if argv is not None else sys.argv[1:]
    if not args or args[0] not in {"upgrade", "downgrade", "current", "check"}:
        print("usage: python -m tabayyun.db.migrate upgrade [rev] | downgrade <rev> | current | check")  # noqa: T201
        return 2
    settings: Settings = get_settings()
    cfg = build_config(settings.migration_url)
    if args[0] == "upgrade":
        command.upgrade(cfg, args[1] if len(args) > 1 else "head")
        ensure_app_login(settings.migration_url, settings.database_url)
    elif args[0] == "downgrade":
        if len(args) < 2:
            print("downgrade needs a revision (for example: base or -1)")  # noqa: T201
            return 2
        command.downgrade(cfg, args[1])
    elif args[0] == "current":
        command.current(cfg)
    else:
        command.check(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
