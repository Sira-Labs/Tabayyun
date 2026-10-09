"""A paced, error-mapping wrapper around one PI Web API session (spec 022).

Every request carries `Accept: application/json` and `X-Requested-With` (PI Web API's CSRF
defence refuses POSTs without it), and the source's Basic credentials or bearer token through
the pinned client of the network policy. Responses map to connector errors:

- 401 and 403: `AuthError`;
- 404 and 410: `PiNotFoundError`, which callers turn into a point failure where one point is
  concerned;
- 429, 500, 502, 503, 504, timeouts and connection errors: retryable;
- a TLS verification failure: not retryable, naming `ca_pem`;
- other 4xx and answers that are not JSON: not retryable, with PI's first error message.

A 2xx answer carrying a `WebException` is treated as the status it names. Messages never carry
credentials: only PI's own error text, cut to 200 characters.
"""

from __future__ import annotations

import ssl
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlencode

import httpx

from tabayyun.connectors.errors import AuthError, ConnectorError
from tabayyun.connectors.net import NetPolicy
from tabayyun.connectors.pi_web_api.config import PiWebApiConfig, PiWebApiCredentials
from tabayyun.connectors.plan import Pacer

RETRYABLE = frozenset({429, 500, 502, 503, 504})
MAX_MESSAGE = 200
Params = Mapping[str, str | int | list[str]]


class PiNotFoundError(ConnectorError):
    """PI Web API has no such item (404, 410)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


def _first_error(body: Any) -> str | None:
    if isinstance(body, Mapping):
        errors = body.get("Errors")
        if isinstance(errors, list) and errors:
            return " ".join(str(errors[0]).split())[:MAX_MESSAGE]
        web = body.get("WebException")
        if isinstance(web, Mapping):
            return _first_error(web)
    return None


def raise_for(status: int, body: Any, what: str) -> None:
    """The connector error a PI status (and body) stands for; nothing for a success."""
    if 200 <= status < 300:
        return
    detail = _first_error(body)
    suffix = f": {detail}" if detail else ""
    if status in (401, 403):
        raise AuthError("PI Web API refused the credentials")
    if status in (404, 410):
        raise PiNotFoundError(f"{what} not found{suffix}")
    raise ConnectorError(f"PI Web API answered {status} for {what}{suffix}", retryable=status in RETRYABLE)


def _tls_failure(exc: httpx.HTTPError) -> bool:
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(cause):
            return True
        cause = cause.__cause__ or cause.__context__
    return False


class PiClient:
    """Paced requests to one PI Web API, answers as JSON or connector errors."""

    def __init__(self, http: httpx.AsyncClient, base_url: str, rate: float) -> None:
        self._http = http
        self.base_url = base_url
        self._pacer = Pacer(rate)

    def url(self, path: str, params: Params | None = None) -> str:
        """The absolute URL of `path` (for batch sub-requests, which take absolute resources)."""
        query = f"?{urlencode(params, doseq=True)}" if params else ""
        return f"{self.base_url}/{path}{query}"

    async def get(self, path: str, params: Params | None = None, *, what: str) -> Any:
        """GET `path` relative to the base URL."""
        return await self._send("GET", path, what=what, params=params)

    async def post(self, path: str, body: Any, *, what: str) -> Any:
        """POST JSON to `path` relative to the base URL."""
        return await self._send("POST", path, what=what, json=body)

    async def _send(self, method: str, path: str, *, what: str, **kwargs: Any) -> Any:
        await self._pacer.wait()
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.TimeoutException:
            raise ConnectorError(f"PI Web API timed out on {what}", retryable=True) from None
        except httpx.HTTPError as exc:
            if _tls_failure(exc):
                raise ConnectorError(
                    "PI Web API's TLS certificate is not trusted; set ca_pem to its certificate authority",
                    retryable=False,
                ) from None
            raise ConnectorError(f"cannot reach PI Web API ({type(exc).__name__})", retryable=True) from None
        try:
            body = response.json()
        except ValueError:
            raise_for(response.status_code, None, what)
            raise ConnectorError(
                f"PI Web API answered something other than JSON for {what}", retryable=False
            ) from None
        raise_for(response.status_code, body, what)
        web = body.get("WebException") if isinstance(body, Mapping) else None
        if isinstance(web, Mapping) and isinstance(web.get("StatusCode"), int):
            raise_for(web["StatusCode"], web, what)
        return body


@asynccontextmanager
async def open_client(
    config: PiWebApiConfig, credentials: PiWebApiCredentials | None, net: NetPolicy
) -> AsyncIterator[PiClient]:
    """A PI client over a pinned connection that closes on exit.

    Raises:
        AuthError: the source has no credentials.
        TargetRefusedError: the network policy refuses the host.
    """
    if credentials is None:
        raise AuthError("no credentials set for this source")
    headers = {"Accept": "application/json", "X-Requested-With": "tabayyun"}
    auth: httpx.Auth | None = None
    if credentials.kind == "basic":
        assert credentials.username is not None and credentials.password is not None
        auth = httpx.BasicAuth(credentials.username, credentials.password)
    else:
        headers["Authorization"] = f"Bearer {credentials.token}"
    http = await net.http_client(config.base_url, verify=config.ssl_context(), headers=headers, auth=auth)
    async with http:
        yield PiClient(http, config.base_url, config.requests_per_second)
