"""Errors of the admin services, each carrying the status and the `detail` code the API answers."""

from __future__ import annotations


class AdminError(Exception):
    """A refused admin request; `create_app` turns it into `{"detail": code}` with `status`."""

    status = 400

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class NotFoundError(AdminError):
    """The id does not exist for the caller (another org's ids included): 404."""

    status = 404

    def __init__(self, code: str = "not found") -> None:
        super().__init__(code)


class ForbiddenError(AdminError):
    """The caller's role does not allow the change: 403 `forbidden`."""

    status = 403

    def __init__(self, code: str = "forbidden") -> None:
        super().__init__(code)


class ConflictError(AdminError):
    """The change contradicts the current state, e.g. `last_owner`, `name_taken`: 409."""

    status = 409


class InvalidError(AdminError):
    """A value is not acceptable, e.g. `invalid_email`, `invalid_timezone`: 422."""

    status = 422
