"""Connector errors (spec 021): the only exceptions the fetch engine expects from a connector."""

from __future__ import annotations


class ConnectorError(Exception):
    """What a connector raises. `retryable` says whether trying again later may succeed.

    The message is shown to admins and logged: name the source or the point, never a credential.
    """

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class AuthError(ConnectorError):
    """The system refused the credentials; retrying will not help."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


class NotSupportedError(ConnectorError):
    """The connector cannot do this (for example, it has no point search)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


class TargetRefusedError(ConnectorError):
    """The target resolves to an address the network policy refuses, or does not resolve."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)
