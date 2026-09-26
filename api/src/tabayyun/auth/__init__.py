"""Login for the web app (spec 013): OIDC against the Keycloak realm as a backend-for-frontend,
sessions in Postgres behind an HttpOnly cookie, the CSRF guard and the passkey gate."""

from tabayyun.auth.csrf import CsrfMiddleware
from tabayyun.auth.deps import (
    LOGIN_COOKIE,
    SESSION_COOKIE,
    current_session,
    require_recent_passkey,
    require_session,
)
from tabayyun.auth.oidc import OidcClient
from tabayyun.settings import Settings


def build_oidc(settings: Settings) -> OidcClient | None:
    """The OIDC client in oidc mode, None in dev mode.

    Outside prod (where `require_secrets_in_prod` already checked them) a missing setting
    stops the start too, naming it, rather than failing at the first login.
    """
    if settings.resolved_auth_mode == "dev":
        return None
    missing = [
        name
        for name, value in (
            ("TABAYYUN_PUBLIC_URL", settings.public_url),
            ("TABAYYUN_OIDC_ISSUER", settings.oidc_issuer),
            ("TABAYYUN_OIDC_CLIENT_SECRET", settings.oidc_client_secret),
            ("TABAYYUN_SESSION_SECRET", settings.session_secret),
        )
        if not value
    ]
    if missing or settings.oidc_issuer is None or settings.oidc_client_secret is None:
        raise RuntimeError(f"TABAYYUN_AUTH_MODE=oidc needs {', '.join(missing)}")
    return OidcClient(
        settings.oidc_issuer, settings.oidc_client_id, settings.oidc_client_secret.get_secret_value()
    )


__all__ = [
    "LOGIN_COOKIE",
    "SESSION_COOKIE",
    "CsrfMiddleware",
    "OidcClient",
    "build_oidc",
    "current_session",
    "require_recent_passkey",
    "require_session",
]
