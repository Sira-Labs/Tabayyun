# ADR-0018: Use asyncua (LGPL-3.0) for the OPC UA connector

- **Status:** Accepted
- **Date:** 2026-10-10
- **Deciders:** owner

## Context

The OPC UA connector (S9-3, spec 023) needs an OPC UA client with Basic256Sha256 security,
browsing and history reads. The project is Apache-2.0 (ADR-0012), which asks dependencies to
stay Apache-compatible. In Python, the maintained client is `asyncua` (opcua-asyncio, 2.1.0),
licensed **LGPL-3.0-or-later**. The Rust alternative, `async-opcua`, is MPL-2.0
(`docs/research/04-backend-core-stack.md`), but it would need new PyO3 bindings in the core
and several more days of work.

## Decision

Use `asyncua` as an unmodified, separately installed Python package.

- It is a normal dependency in `api/pyproject.toml`, installed from PyPI into the image's
  virtual environment. It is imported, never vendored, copied or patched.
- The LGPL lets an Apache-2.0 program use the library this way. A user can replace the
  installed package, and the project's own code stays under Apache-2.0.
- The image ships the package as installed. Its licence and source are those on PyPI, which
  meets the LGPL's requirement to make the library's source available.

## Alternatives considered

| Option | Pros | Cons | Why not |
|---|---|---|---|
| `async-opcua` (Rust, MPL-2.0) through PyO3 | permissive file-level licence, fast | new bindings for browse, history and security; several days more | S9-3 fits the sprint with asyncua; revisit if throughput or licence policy demands |
| `opcua` (python-opcua, LGPL) | older API | unmaintained, synchronous | same licence, worse library |
| A commercial SDK | support contract | cost, licence keys on customer sites | not for a self-hosted open-source tool |

## Consequences

- Patching asyncua would bring LGPL obligations for the patched copy. Fixes go upstream, or
  into our own code around the library.
- The planned `pip-licenses` check (ADR-0012 follow-up) allows `LGPL-3.0-or-later` for
  `asyncua` only.
- `NOTICE` and the deployment docs name asyncua and its licence.
