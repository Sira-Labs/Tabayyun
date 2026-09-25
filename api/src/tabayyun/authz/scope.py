"""Who acts and where (spec 007): kept free of imports so services can take a `Scope`."""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    """The user a request or a job acts for, in the org it acts in."""

    user_id: uuid.UUID
    org_id: uuid.UUID


@dataclass(frozen=True)
class Scope:
    """The org and workspace a service call reads and writes; authorized by the caller."""

    org_id: uuid.UUID
    workspace_id: uuid.UUID
