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
        """Refuse to start in prod with placeholder or missing secrets."""
        if self.env != "prod":
            return
        missing = [
            name
            for name, value in (
                ("TABAYYUN_OIDC_ISSUER", self.oidc_issuer),
                ("TABAYYUN_OIDC_CLIENT_ID", self.oidc_client_id),
                ("TABAYYUN_OIDC_CLIENT_SECRET", self.oidc_client_secret),
                ("TABAYYUN_SESSION_SECRET", self.session_secret),
            )
            if not value
        ]
        if missing or "tabayyun:tabayyun@" in self.database_url:
            raise RuntimeError(f"refusing to start in prod: missing/placeholder settings {missing}")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
