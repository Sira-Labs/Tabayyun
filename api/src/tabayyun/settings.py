"""Runtime configuration. Everything comes from environment variables or a mounted secret
file; nothing here has a real default for a secret (see docs/frontend/02-security-baseline.md).
"""

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TABAYYUN_", env_file=".env", extra="ignore")

    env: str = Field(default="dev", description="dev | test | prod")
    log_level: str = "INFO"
    database_url: str = Field(
        default="postgresql+psycopg://tabayyun:tabayyun@localhost:5432/tabayyun",
        description="SQLAlchemy async URL. Override in every non-dev environment.",
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
    oidc_client_id: str | None = None
    oidc_client_secret: SecretStr | None = None
    session_secret: SecretStr | None = Field(default=None, description="Key for session-store signing.")

    def require_secrets_in_prod(self) -> None:
        """Refuse to start in prod with placeholder or missing secrets.

        OIDC settings become mandatory once the auth router ships; until then they are
        validated only when set (a placeholder value is still rejected).
        """
        if self.env != "prod":
            return
        problems: list[str] = []
        if not self.session_secret or _is_placeholder(self.session_secret.get_secret_value()):
            problems.append("TABAYYUN_SESSION_SECRET")
        if "tabayyun:tabayyun@" in self.database_url or _is_placeholder(self.database_url):
            problems.append("TABAYYUN_DATABASE_URL")
        if self.oidc_client_secret and _is_placeholder(self.oidc_client_secret.get_secret_value()):
            problems.append("TABAYYUN_OIDC_CLIENT_SECRET")
        if self.s3_secret_access_key and _is_placeholder(self.s3_secret_access_key.get_secret_value()):
            problems.append("TABAYYUN_S3_SECRET_ACCESS_KEY")
        if self.cache_url.startswith("s3://") and not (self.s3_access_key_id and self.s3_secret_access_key):
            problems.append("TABAYYUN_S3_ACCESS_KEY_ID/TABAYYUN_S3_SECRET_ACCESS_KEY")
        if problems:
            raise RuntimeError(f"refusing to start in prod: missing or placeholder settings {problems}")


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
