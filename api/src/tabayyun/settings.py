"""Runtime configuration. Everything comes from environment variables or a mounted secret
file; nothing here has a real default for a secret (see docs/frontend/02-security-baseline.md).
"""

import re
from datetime import timedelta
from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SIGN_IN_METHODS = ("google", "github", "passkey")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TABAYYUN_", env_file=".env", extra="ignore")

    env: str = Field(default="dev", description="dev | test | prod")
    log_level: str = "INFO"
    commit: str | None = Field(
        default=None,
        description="Git commit the image was built from; the image build sets it, and the "
        "release workflow compares it after a deploy.",
    )
    database_url: str = Field(
        default="postgresql+psycopg://tabayyun:tabayyun@localhost:5432/tabayyun",
        description="SQLAlchemy async URL of the app login (a member of tabayyun_app, spec 007) that "
        "the api and the worker use. Override in every non-dev environment.",
    )
    migration_database_url: str | None = Field(
        default=None,
        description="URL of the table owner, used only by `python -m tabayyun.db.migrate`; unset "
        "means `database_url` (a single dev login).",
    )
    db_pool_size: int = Field(default=5, ge=1, description="Connection pool size per process.")
    db_pool_max_overflow: int = Field(default=10, ge=0, description="Extra connections beyond the pool size.")
    timescale: Literal["auto", "on", "off"] = Field(
        default="auto",
        description="auto: use TimescaleDB when the extension is available; on: require it; "
        "off: never create hypertables (Apache-2-only mode, ADR-0003).",
    )
    inline_jobs: bool = Field(
        default=False,
        description="Execute jobs in the API process (tests, single-process dev) instead of enqueueing them.",
    )
    worker_concurrency: int = Field(default=2, ge=1, description="Jobs one worker process runs concurrently.")
    test_database_url: str | None = Field(
        default=None,
        description="When set, pytest runs the database tests against this URL; otherwise they skip.",
    )
    cache_url: str = Field(
        default="./data/cache",
        # TABAYYUN_CACHE_DIR is the pre-spec-006 name, still set by older deployments.
        validation_alias=AliasChoices("TABAYYUN_CACHE_URL", "TABAYYUN_CACHE_DIR", "cache_url"),
        description="Parquet cache root: a local path, file:// or s3://bucket[/prefix] (spec 006).",
    )
    s3_endpoint: str | None = Field(default=None, description="S3 endpoint URL; unset means AWS.")
    s3_region: str = "us-east-1"
    s3_access_key_id: str | None = None
    s3_secret_access_key: SecretStr | None = None
    s3_allow_http: bool = Field(
        default=False, description="Allow a plain-http S3 endpoint (internal network only)."
    )
    oidc_issuer: str | None = Field(default=None, description="OIDC issuer URL of the identity provider.")
    oidc_client_id: str = Field(default="tabayyun-api", description="Confidential client in the realm.")
    oidc_client_secret: SecretStr | None = None
    session_secret: SecretStr | None = Field(
        default=None, description="Key of the HMAC under which session and login-flow tokens are stored."
    )
    auth_mode: Literal["oidc", "dev"] | None = Field(
        default=None,
        description="oidc: sessions required (spec 013); dev: every request acts as the bootstrap user. "
        "Unset means oidc in prod and dev elsewhere; prod refuses dev.",
    )
    public_url: str | None = Field(
        default=None, description="External base URL; redirect URIs and the Origin check derive from it."
    )
    sign_in_methods: str = Field(
        default="google,github,passkey",
        description="Comma-separated sign-in buttons: google, github, passkey.",
    )
    admin_email: str | None = Field(
        default=None,
        description="Becomes owner of the default org at its first verified login while that org has no "
        "other owner (spec 013).",
    )
    session_idle: timedelta = Field(default=timedelta(hours=12), description="Idle timeout, e.g. 12h.")
    session_absolute: timedelta = Field(
        default=timedelta(days=30), description="Absolute lifetime, e.g. 30d."
    )
    passkey_fresh: timedelta = Field(
        default=timedelta(hours=12), description="How recent a passkey sign-in must be for admin actions."
    )

    @field_validator("session_idle", "session_absolute", "passkey_fresh", mode="before")
    @classmethod
    def _duration(cls, value: object) -> object:
        """Accept `30s`, `15m`, `12h` and `30d` besides pydantic's own duration forms."""
        if isinstance(value, str) and (m := re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", value)):
            unit = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[m.group(2)]
            return timedelta(**{unit: int(m.group(1))})
        return value

    @field_validator("sign_in_methods")
    @classmethod
    def _methods(cls, value: str) -> str:
        """Only known methods, at least one."""
        methods = [m.strip() for m in value.split(",") if m.strip()]
        unknown = sorted(set(methods) - set(SIGN_IN_METHODS))
        if unknown or not methods:
            raise ValueError(f"sign_in_methods must name some of {', '.join(SIGN_IN_METHODS)}; got {value!r}")
        return ",".join(dict.fromkeys(methods))

    @property
    def resolved_auth_mode(self) -> Literal["oidc", "dev"]:
        """The auth mode in force: the setting, else oidc in prod and dev elsewhere."""
        if self.auth_mode is not None:
            return self.auth_mode
        return "oidc" if self.env == "prod" else "dev"

    @property
    def enabled_sign_in_methods(self) -> tuple[str, ...]:
        """The sign-in methods the login page offers, in the configured order."""
        return tuple(self.sign_in_methods.split(","))

    @property
    def migration_url(self) -> str:
        """The owner URL migrations run with: `migration_database_url`, else `database_url`."""
        return self.migration_database_url or self.database_url

    def require_secrets_in_prod(self) -> None:
        """Refuse to start in prod with placeholder or missing secrets.

        With the OIDC login (the prod default, spec 013) the public URL, the issuer, the client
        secret and the admin email are required too; the dev auth mode is refused.
        """
        if self.env != "prod":
            return
        problems: list[str] = []
        if not self.session_secret or _is_placeholder(self.session_secret.get_secret_value()):
            problems.append("TABAYYUN_SESSION_SECRET")
        if "tabayyun:tabayyun@" in self.database_url or _is_placeholder(self.database_url):
            problems.append("TABAYYUN_DATABASE_URL")
        if self.migration_database_url and (
            "tabayyun:tabayyun@" in self.migration_database_url
            or _is_placeholder(self.migration_database_url)
        ):
            problems.append("TABAYYUN_MIGRATION_DATABASE_URL")
        if self.oidc_client_secret and _is_placeholder(self.oidc_client_secret.get_secret_value()):
            problems.append("TABAYYUN_OIDC_CLIENT_SECRET")
        if self.resolved_auth_mode == "dev":
            problems.append("TABAYYUN_AUTH_MODE (dev is not allowed in prod)")
        else:
            problems += self._oidc_problems()
        if self.s3_secret_access_key and _is_placeholder(self.s3_secret_access_key.get_secret_value()):
            problems.append("TABAYYUN_S3_SECRET_ACCESS_KEY")
        if self.cache_url.startswith("s3://") and not (self.s3_access_key_id and self.s3_secret_access_key):
            problems.append("TABAYYUN_S3_ACCESS_KEY_ID/TABAYYUN_S3_SECRET_ACCESS_KEY")
        if problems:
            raise RuntimeError(f"refusing to start in prod: missing or placeholder settings {problems}")

    def _oidc_problems(self) -> list[str]:
        """Settings the OIDC login needs in prod (spec 013)."""
        problems: list[str] = []
        url = urlsplit(self.public_url or "")
        if url.scheme != "https" or not url.netloc or url.path not in ("", "/"):
            problems.append("TABAYYUN_PUBLIC_URL (an https origin)")
        if not (self.oidc_issuer or "").startswith("https://"):
            problems.append("TABAYYUN_OIDC_ISSUER")
        if not self.oidc_client_secret:
            problems.append("TABAYYUN_OIDC_CLIENT_SECRET")
        if not (self.admin_email or "").strip():
            # Without it no login could become owner: the bootstrap owner has no login.
            problems.append("TABAYYUN_ADMIN_EMAIL")
        return problems


def _is_placeholder(value: str) -> bool:
    """True for change-me style values and anything shorter than 16 characters."""
    lowered = value.lower()
    return (
        "change-me" in lowered or "changeme" in lowered or "placeholder" in lowered or len(value.strip()) < 16
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, read once from the environment."""
    return Settings()
