"""`python -m tabayyun.secrets rotate`: re-encrypt connector credentials under the current key.

Set the new key as `TABAYYUN_MASTER_KEY` and the old one as `TABAYYUN_MASTER_KEY_PREVIOUS`,
run this once (as the table owner, through `TABAYYUN_MIGRATION_DATABASE_URL` when set), then
drop the previous key. Prints the number of rows re-encrypted.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tabayyun.secrets import CredentialsError, Keyring, rotate
from tabayyun.settings import get_settings


async def _rotate() -> int:
    settings = get_settings()
    keyring = Keyring.from_settings(settings)
    if keyring is None:
        sys.stderr.write("TABAYYUN_MASTER_KEY is not set\n")
        return 2
    engine = create_async_engine(settings.migration_url)
    try:
        async with async_sessionmaker(engine)() as session, session.begin():
            count = await rotate(session, keyring)
    except CredentialsError as exc:
        sys.stderr.write(f"rotation stopped, nothing changed: {exc}\n")
        return 1
    finally:
        await engine.dispose()
    sys.stdout.write(f"{count}\n")
    return 0


def main() -> int:
    """Parse the command and run it."""
    parser = argparse.ArgumentParser(prog="python -m tabayyun.secrets")
    parser.add_argument("command", choices=["rotate"])
    parser.parse_args()
    return asyncio.run(_rotate())


if __name__ == "__main__":
    raise SystemExit(main())
