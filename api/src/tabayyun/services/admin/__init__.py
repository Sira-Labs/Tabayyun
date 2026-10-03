"""Tenant administration (spec 014): org, members, invitations, teams, workspaces and the audit
log. Routes authorize and hand over an `Actor`; every mutation writes its audit event in the
caller's transaction, so a failed change leaves no event."""
