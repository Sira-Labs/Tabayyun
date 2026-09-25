"""Test helpers for the tenant context (spec 007).

Database tests run the app as a non-owner login in `tabayyun_app`, so row-level security is
exercised rather than bypassed; migrations and fixtures that must see every org use the owner
URL (`TABAYYUN_TEST_DATABASE_URL`).
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from tabayyun.db import for_org
from tabayyun.db.models import DEFAULT_ORG_ID
from tabayyun.settings import Settings

# A throwaway login on the local test server, created by the migrate step; not a secret.
APP_LOGIN = "tabayyun_app_test"
APP_PASSWORD = "app-login-for-tests-only"


def app_url(owner_url: str) -> str:
    """The owner URL with the app login in place of the owner."""
    url = make_url(owner_url).set(username=APP_LOGIN, password=APP_PASSWORD)
    return url.render_as_string(hide_password=False)


def app_settings(owner_url: str, **overrides: Any) -> Settings:
    """Test settings: the app login for requests and jobs, the owner for migrations."""
    return Settings(
        env="test", database_url=app_url(owner_url), migration_database_url=owner_url, **overrides
    )


def org_session(app: FastAPI, org_id: uuid.UUID = DEFAULT_ORG_ID) -> AsyncSession:
    """A session of `app` in `org_id`'s tenant context (the default org unless given)."""
    return for_org(app.state.session_factory, org_id)()
