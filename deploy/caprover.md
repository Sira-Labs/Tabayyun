# Deploying on CapRover

CapRover already provides the reverse proxy, TLS and container scheduling, so the compose
bundle is not used there (its Caddy would fight CapRover's nginx for ports 80/443). Instead,
three CapRover apps run the published images; CapRover's nginx terminates TLS and the web
app's Caddy proxies `/api` to the API app over the internal network.

```
Internet ──▶ CapRover nginx (TLS) ──▶ tabayyun-web (Caddy :80) ──/api──▶ tabayyun-api (:8000)
                                                                              │
                                                                     tabayyun-db (TimescaleDB)
```

## 1. Database app: `tabayyun-db`

- Apps → One-Click Apps/Databases is **not** used (it lacks TimescaleDB). Create a plain app
  named `tabayyun-db` with **Has Persistent Data** ticked.
- Deployment tab → *Deploy via ImageName*: `timescale/timescaledb:2.30.1-pg17`
- App Configs:
  - Environment variables: `POSTGRES_USER=tabayyun`, `POSTGRES_PASSWORD=<strong>`,
    `POSTGRES_DB=tabayyun`
  - Persistent directory: path in app `/var/lib/postgresql/data`, label `tabayyun-pgdata`
  - Do not map a host port; the API reaches it as `srv-captain--tabayyun-db:5432`.

## 2. API app: `tabayyun-api`

- Create app `tabayyun-api` (persistent data ticked for the Parquet cache).
- App Configs:
  - Environment variables:

    | Name | Value |
    |---|---|
    | `TABAYYUN_ENV` | `prod` |
    | `TABAYYUN_DATABASE_URL` | `postgresql+psycopg://tabayyun:<password>@srv-captain--tabayyun-db:5432/tabayyun` |
    | `TABAYYUN_SESSION_SECRET` | output of `openssl rand -base64 48` |
    | `TABAYYUN_CACHE_DIR` | `/data/cache` |

  - Persistent directory: `/data/cache`, label `tabayyun-cache`
  - Container HTTP port: `8000`
- HTTP Settings: no public domain needed (the web app proxies to it). If you want the API
  reachable directly, enable HTTPS on its default domain.
- Deployment tab → **Enable App Token**, copy it (used by CI below). For the first deploy,
  *Deploy via ImageName*: `ghcr.io/thedatadudech/tabayyun-api:latest`.

## 3. Web app: `tabayyun-web`

- Create app `tabayyun-web` (no persistent data).
- App Configs → Environment variables: `TABAYYUN_API_UPSTREAM=srv-captain--tabayyun-api:8000`
  (Caddy inside the image proxies `/api` and `/healthz` there; `TABAYYUN_DOMAIN` stays
  unset so Caddy serves plain HTTP on :80 behind CapRover).
- Container HTTP port: `80`.
- HTTP Settings: connect your domain (e.g. `tabayyun.example.com`), Enable HTTPS, Force HTTPS.
- Deployment tab → **Enable App Token**, copy it. First deploy via ImageName:
  `ghcr.io/thedatadudech/tabayyun-web:latest`.

Open the domain: the page should show the API version and let you upload a CSV.

## 4. Continuous deployment from GitHub Actions

The `deploy-caprover` job in `.github/workflows/release.yml` deploys the images built by
each push to `main` (tag `sha-<short>`) using CapRover's official action. Configure in the
repository → Settings → Secrets and variables → Actions:

| Kind | Name | Value |
|---|---|---|
| variable | `CAPROVER_SERVER` | `https://captain.<your-root-domain>` |
| variable | `CAPROVER_APP_API` | `tabayyun-api` (optional, default) |
| variable | `CAPROVER_APP_WEB` | `tabayyun-web` (optional, default) |
| secret | `CAPROVER_APP_TOKEN_API` | app token from the API app's Deployment tab |
| secret | `CAPROVER_APP_TOKEN_WEB` | app token from the web app's Deployment tab |

The job is skipped until `CAPROVER_SERVER` exists. Images are public on GHCR, so CapRover
needs no registry credentials; if the repository ever becomes private, add the registry
under CapRover → Cluster → Docker Registries first.

## 5. Alternative: let CapRover build from GitHub (Method 3)

Instead of pulling images from GHCR, each app can clone the repository and build its own
image on the server. Both Dockerfiles use the repository root as build context, so the
`captain-definition` files under `deploy/caprover/` point at them:

| App | Deployment tab → captain-definition Relative Path |
|---|---|
| `tabayyun-api` | `./deploy/caprover/api/captain-definition` |
| `tabayyun-web` | `./deploy/caprover/web/captain-definition` |

In each app's Deployment tab fill in *Repository* (`github.com/thedatadudech/Tabayyun`),
*Branch* (`main`) and, because the repository is public, no username/password. Save, copy
the generated webhook URL, and add it on GitHub under Settings → Webhooks (content type
JSON, "just the push event"). Every push to `main` then rebuilds and redeploys both apps.
The database app keeps using *Deploy via ImageName*.

Trade-offs against the GHCR path in section 4:

- Requires CapRover ≥ 1.15.0: the Dockerfiles use BuildKit cache mounts and CapRover only
  builds with BuildKit from that release on. Check the version under Settings.
- The Rust wheel compiles on your Hetzner server (several minutes, roughly 2–4 GB RAM at
  peak) on every push, next to the running apps. The GHCR path does that on GitHub runners.
- It deploys whatever `main` currently is, not the immutable `sha-<short>` tag the release
  workflow published, so rolling back means pushing a revert.
- No repository secrets or app tokens are needed; the `deploy-caprover` job stays skipped
  as long as `CAPROVER_SERVER` is unset.

Do not enable both paths for the same app, or each push deploys it twice.

## Notes

- Keep the Hetzner firewall closed except 80/443 (CapRover) and your SSH port.
- Backups: CapRover persistent volumes live under `/captain/data/`; snapshot the server or
  `pg_dump` from a one-off container on the same network.
- Resource guidance for a pilot: 2 vCPU / 4 GB is enough for the API and web; give the
  database its own volume and watch disk for the Parquet cache.
