# ADR-0006: External OIDC identity provider (Keycloak default, Zitadel supported)

- **Status:** Accepted
- **Date:** 2026-09-21

## Context
Requirements: Google login, enterprise SSO (Entra ID, Okta, SAML), passkeys, MFA, later
SCIM, self-hostable, no password storage in Tabayyun.

## Decision
Tabayyun is an OIDC relying party only. The compose bundle ships Keycloak 26.x
(Apache-2.0) preconfigured with a Tabayyun realm; Zitadel is a documented alternative for
customers preferring its organisation model. Google is a brokered IdP; each organisation may
attach its own OIDC/SAML connection. Organisation and role claims are mapped from IdP groups
at login and reconciled into Tabayyun's membership tables.

## Alternatives considered
| Option | Pros | Cons | Why not |
|---|---|---|---|
| Better Auth / Auth.js | Fast for TS apps | TypeScript-only, needs a Node auth service beside FastAPI | Second runtime |
| Clerk / Auth0 / WorkOS | Turnkey | Hosted; contradicts self-hosting | Optional cloud tier later |
| Ory Kratos + Hydra | Flexible | Build your own UI and flows | Effort |
| Hand-rolled auth in FastAPI | No extra service | Password storage, MFA, SAML all on us | Risk |

## Consequences
One more container in the bundle (Keycloak ~600 MB RSS). SCIM is preview on both IdPs;
promise it only after testing with Entra/Okta.
