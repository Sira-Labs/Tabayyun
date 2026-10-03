"""The owner rules for org roles (spec 014), pure so they can be tested on their own.

Only an owner grants `owner`, changes an owner's role or removes an owner; the last active
owner (the bootstrap user does not count) can be neither demoted nor removed.
"""

from __future__ import annotations

from tabayyun.authz import OrgRole
from tabayyun.services.admin.errors import ConflictError, ForbiddenError


def check_role_change(actor: OrgRole, target: OrgRole, new: OrgRole | None, owners: int) -> None:
    """Raise unless `actor` may move a member from `target` to `new` (None: removal).

    `owners` is the number of active owners, the target included when they are one.
    """
    if actor not in (OrgRole.OWNER, OrgRole.ADMIN):
        raise ForbiddenError
    if OrgRole.OWNER in (target, new) and actor is not OrgRole.OWNER:
        raise ForbiddenError
    if target is OrgRole.OWNER and new is not OrgRole.OWNER and owners <= 1:
        raise ConflictError("last_owner")
