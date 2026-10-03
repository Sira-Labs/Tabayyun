"""Name and timezone validation shared by teams, workspaces and the org."""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tabayyun.services.admin.errors import InvalidError

MAX_NAME = 100


def clean_name(name: str) -> str:
    """The trimmed name, 1–100 characters; InvalidError("invalid_name") otherwise."""
    cleaned = " ".join(name.split())
    if not 1 <= len(cleaned) <= MAX_NAME:
        raise InvalidError("invalid_name")
    return cleaned


def clean_timezone(tz: str) -> str:
    """An IANA zone known to `zoneinfo`; InvalidError("invalid_timezone") otherwise."""
    tz = tz.strip()
    # ZoneInfo accepts paths such as "../x" only to fail later; keep to plain zone keys.
    if not tz or tz.startswith("/") or ".." in tz:
        raise InvalidError("invalid_timezone")
    try:
        ZoneInfo(tz)
    # A key naming a directory ("Europe") raises IsADirectoryError or PermissionError on some
    # Python and tzdata versions instead of ZoneInfoNotFoundError.
    except (ZoneInfoNotFoundError, ValueError, IsADirectoryError, PermissionError) as exc:
        raise InvalidError("invalid_timezone") from exc
    return tz
