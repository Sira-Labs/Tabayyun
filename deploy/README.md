# Deployment

Two images are built by `.github/workflows/release.yml` on every push to `main` and on
`v*` tags, and published to the GitHub Container Registry with provenance and SBOM:

| Image | Contents |
|---|---|
| `ghcr.io/thedatadudech/tabayyun-api` | Python API with the Rust core wheel built in-image (no compiler at runtime) |
| `ghcr.io/thedatadudech/tabayyun-web` | Static SPA served by Caddy with security headers; proxies `/api` to the API |

Tags: `latest` (main), `main`, `sha-<short>`, and `<version>` for tags. The compose bundle
requires `TABAYYUN_TAG`; the CD job sets it to the `sha-<short>` tag of the images it just
built, so a deployment never depends on a moving tag. The TimescaleDB image is pinned by
digest; bump it deliberately, together with a database upgrade note.

## Run on CapRover

See [caprover.md](caprover.md): three apps from the published images, TLS by CapRover,
and a `deploy-caprover` job in the release workflow.

## Run on any Docker host

```bash
mkdir -p ~/tabayyun && cd ~/tabayyun
curl -fsSL https://raw.githubusercontent.com/thedatadudech/Tabayyun/main/deploy/compose.yaml -o compose.yaml
curl -fsSL https://raw.githubusercontent.com/thedatadudech/Tabayyun/main/deploy/.env.example -o .env
# edit .env: POSTGRES_PASSWORD and TABAYYUN_SESSION_SECRET (both mandatory, generate with
# `openssl rand -base64 36`), TABAYYUN_DOMAIN for automatic TLS; OIDC values stay empty
# until the auth router ships
docker compose up -d
```

Caddy obtains a TLS certificate automatically when `TABAYYUN_DOMAIN` is set and ports 80/443
are reachable. Without a domain it serves plain HTTP on port 80 for use behind your own
load balancer.

## Continuous deployment from GitHub Actions

The `deploy` job in the release workflow ships the compose file to a host over SSH and runs
`docker compose pull && up -d`. Its steps are skipped until you configure a GitHub
**environment** named `production` with:

| Kind | Name | Value |
|---|---|---|
| variable | `DEPLOY_HOST` | hostname or IP of the Docker host |
| variable | `DEPLOY_USER` | SSH user with permission to run docker |
| secret | `DEPLOY_SSH_KEY` | private key for that user (use a dedicated deploy key) |

Repository → Settings → Environments → New environment → `production`. Add a required
reviewer there if you want a manual approval gate before each deployment.

The host needs Docker Engine with the compose plugin and outbound access to `ghcr.io`.
The job logs in to the registry with the workflow's own token, which works for public
packages and for private packages of the same repository.

## Air-gapped installs

```bash
docker pull ghcr.io/thedatadudech/tabayyun-api:latest ghcr.io/thedatadudech/tabayyun-web:latest timescale/timescaledb:latest-pg17
docker save ghcr.io/thedatadudech/tabayyun-api:latest ghcr.io/thedatadudech/tabayyun-web:latest timescale/timescaledb:latest-pg17 | zstd > tabayyun-images.tar.zst
# on the target host
zstd -d < tabayyun-images.tar.zst | docker load
```

## Local development

`deploy/compose.dev.yaml` starts only Postgres+TimescaleDB and Keycloak; run the API and web
on the host (`make db-upgrade` once, then `make api-dev`, `make web-dev`).

## Schema migrations

The api image runs `python -m tabayyun.db.migrate upgrade head` (the packaged Alembic
migrations) on every start, before uvicorn, so a deployment
upgrades the schema in place; the migration is idempotent and a failed one stops the new
container while the previous release keeps serving. The API refuses to serve (exit code 3)
when the database is at a different revision than the migrations it ships with, and
`GET /api/version` reports `schema_revision`. `TABAYYUN_TIMESCALE` (`auto` by default)
controls whether findings, metrics and scores become hypertables: `on` requires the
extension, `off` never uses it (Apache-2-only mode, ADR-0003). Take a `pg_dump` before
upgrading a production database.
