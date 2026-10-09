"""Config and credentials of a `pi_web_api` source (spec 022).

The config names where PI Web API is and which PI Data Archive (and optionally which AF
database) the source reads; `ca_pem` trusts a private certificate authority. Credentials are
Basic or a bearer token; Kerberos is out of scope.
"""

from __future__ import annotations

import re
import ssl
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tabayyun.connectors.base import ConnectorConfig

DATA_SERVER = re.compile(r"^\\\\[^\\/|]{1,253}$")
ASSET_DATABASE = re.compile(r"^\\\\[^\\/|]{1,253}\\[^\\|]{1,400}$")
# RFC 6750 `b64token`: no spaces or line breaks can reach the Authorization header.
BEARER = re.compile(r"^[A-Za-z0-9\-._~+/]+=*$")
MAX_CA_PEM = 64 * 1024


class PiWebApiConfig(ConnectorConfig):
    """Where PI Web API is, what it reads, and how hard to push it."""

    base_url: str = Field(max_length=500)
    data_server: str = Field(max_length=256)
    asset_database: str | None = Field(default=None, max_length=656)
    ca_pem: str | None = Field(default=None, max_length=MAX_CA_PEM)
    max_count: int = Field(default=10_000, ge=1000, le=150_000)
    # PI Web API's own limit is 1000 requests per second per client; 20 stays polite.
    requests_per_second: float = Field(default=20.0, gt=0, le=50)

    @field_validator("base_url")
    @classmethod
    def _https(cls, value: str) -> str:
        url = urlsplit(value.strip())
        if url.scheme != "https" or not url.hostname:
            raise ValueError("must be an https URL")
        if url.query or url.fragment or "@" in url.netloc:
            raise ValueError("must have no query, fragment or user info")
        return value.strip().rstrip("/")

    @field_validator("data_server")
    @classmethod
    def _data_server(cls, value: str) -> str:
        if not DATA_SERVER.match(value):
            raise ValueError(r"must be a PI Data Archive path like \\SERVER")
        return value

    @field_validator("asset_database")
    @classmethod
    def _asset_database(cls, value: str | None) -> str | None:
        if value is not None and not ASSET_DATABASE.match(value):
            raise ValueError(r"must be an AF database path like \\AFSERVER\Database")
        return value

    @field_validator("ca_pem")
    @classmethod
    def _ca_pem(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            ssl.create_default_context(cadata=value)
        except (ssl.SSLError, ValueError):
            raise ValueError("must be PEM certificates") from None
        return value

    def ssl_context(self) -> ssl.SSLContext | None:
        """The TLS context trusting `ca_pem` besides the system's store, or None for the default."""
        if self.ca_pem is None:
            return None
        context = ssl.create_default_context()
        context.load_verify_locations(cadata=self.ca_pem)
        return context


class PiWebApiCredentials(BaseModel):
    """`{"kind": "basic", "username", "password"}` or `{"kind": "bearer", "token"}`."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["basic", "bearer"]
    username: str | None = Field(default=None, min_length=1, max_length=256)
    password: str | None = Field(default=None, min_length=1, max_length=1024)
    token: str | None = Field(default=None, min_length=1, max_length=16_384)

    @model_validator(mode="after")
    def _shape(self) -> PiWebApiCredentials:
        if self.kind == "basic":
            if self.username is None or self.password is None or self.token is not None:
                raise ValueError("basic credentials need username and password, and no token")
            if ":" in self.username:
                raise ValueError("username cannot contain ':'")
        else:
            if self.token is None or self.username is not None or self.password is not None:
                raise ValueError("bearer credentials need a token, and no username or password")
            if not BEARER.match(self.token):
                raise ValueError("token must be a bearer token")
        return self
