"""Invitation input rules (spec 014): email normalisation, role combinations and status."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from tabayyun.db.models import Invitation
from tabayyun.services.admin.errors import InvalidError
from tabayyun.services.admin.invitations import Grant, Status, check_grant, normalise_email, status_of

NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    ("raw", "expected"), [("  Ada@Example.ORG ", "ada@example.org"), ("a.b+c@x.co", "a.b+c@x.co")]
)
def test_emails_are_trimmed_and_lower_cased(raw, expected):
    assert normalise_email(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "ada", "ada@", "@example.org", "ada@example", "a da@example.org", "a@b@c.org", "a@x." + "o" * 260],
)
def test_implausible_emails_are_refused(raw):
    with pytest.raises(InvalidError) as err:
        normalise_email(raw)
    assert err.value.code == "invalid_email"


WS = uuid.uuid4()


@pytest.mark.parametrize(
    ("grant", "code"),
    [
        (Grant("owner"), "invalid_role"),
        (Grant("boss"), "invalid_role"),
        (Grant("member", WS, None), "invalid_workspace_grant"),
        (Grant("member", None, "viewer"), "invalid_workspace_grant"),
        (Grant("member", WS, "owner"), "invalid_role"),
    ],
)
def test_bad_grants(grant, code):
    with pytest.raises(InvalidError) as err:
        check_grant(grant)
    assert err.value.code == code


@pytest.mark.parametrize("grant", [Grant("member"), Grant("admin"), Grant("member", WS, "editor")])
def test_good_grants(grant):
    assert check_grant(grant) == grant


@pytest.mark.parametrize(
    ("fields", "status"),
    [
        ({}, Status.PENDING),
        ({"expires_at": NOW}, Status.EXPIRED),
        ({"accepted_at": NOW}, Status.ACCEPTED),
        ({"revoked_at": NOW}, Status.REVOKED),
        ({"accepted_at": NOW, "expires_at": NOW - timedelta(days=1)}, Status.ACCEPTED),
    ],
)
def test_status(fields, status):
    base = {"expires_at": NOW + timedelta(days=1), "accepted_at": None, "revoked_at": None} | fields
    assert status_of(Invitation(**base), NOW) is status
