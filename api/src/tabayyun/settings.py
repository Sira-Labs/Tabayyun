"""Runtime configuration. Everything comes from environment variables or a mounted secret
file; nothing here has a real default for a secret (see docs/frontend/02-security-baseline.md).
"""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TABAYYUN_", env_file=".env", extra="ignore")

    env: str = Field(default="dev", description="dev | test | prod")
    log_level: str = "INFO"
    database_url: str = Field(
        default="postgresql+psycopg://tabayyun:tabayyun@localhost:5432/tabayyun",
        description="SQLAlchemy async URL. Override in every non-dev environment.",
    )
    cache_dir: str = Field(default="./data/cache", description="Parquet cache root (raw + corrected layers).")
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
        if problems:
            raise RuntimeError(f"refusing to start in prod: missing or placeholder settings {problems}")


def _is_placeholder(value: str) -> bool:
    lowered = value.lower()
    return (
        "change-me" in lowered or "changeme" in lowered or "placeholder" in lowered or len(value.strip()) < 16
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
