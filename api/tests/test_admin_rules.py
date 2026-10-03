"""The owner rules of spec 014 as a pure function: who may change or remove whom."""

from __future__ import annotations

import pytest

from tabayyun.authz import OrgRole
from tabayyun.services.admin.errors import ConflictError, ForbiddenError
from tabayyun.services.admin.rules import check_role_change

OWNER, ADMIN, MEMBER = OrgRole.OWNER, OrgRole.ADMIN, OrgRole.MEMBER


@pytest.mark.parametrize(
    ("actor", "target", "new"),
    [
        (OWNER, MEMBER, ADMIN),
        (OWNER, MEMBER, OWNER),
        (OWNER, ADMIN, MEMBER),
        (OWNER, OWNER, ADMIN),
        (OWNER, OWNER, None),
        (ADMIN, MEMBER, ADMIN),
        (ADMIN, ADMIN, MEMBER),
        (ADMIN, MEMBER, None),
        (ADMIN, ADMIN, None),
    ],
)
def test_allowed_with_another_owner_left(actor, target, new):
    check_role_change(actor, target, new, owners=2)


@pytest.mark.parametrize(
    ("actor", "target", "new"),
    [
        (ADMIN, MEMBER, OWNER),  # only owners grant owner
        (ADMIN, OWNER, ADMIN),  # or change an owner
        (ADMIN, OWNER, None),  # or remove one
        (MEMBER, MEMBER, ADMIN),  # members manage nobody
        (MEMBER, MEMBER, None),
    ],
)
def test_forbidden(actor, target, new):
    with pytest.raises(ForbiddenError):
        check_role_change(actor, target, new, owners=2)


@pytest.mark.parametrize("new", [ADMIN, MEMBER, None])
def test_the_last_owner_stays(new):
    with pytest.raises(ConflictError) as err:
        check_role_change(OWNER, OWNER, new, owners=1)
    assert err.value.code == "last_owner"


def test_the_last_owner_keeps_owner():
    """Setting owner on the last owner is not a demotion."""
    check_role_change(OWNER, OWNER, OWNER, owners=1)
