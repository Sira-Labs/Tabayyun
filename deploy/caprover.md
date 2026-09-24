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
  - Environment variables: `POSTGRES_USER=tabayyun`, `POSTGRES_PASSWORD=<generated>`,
    `POSTGRES_DB=tabayyun`. Generate the password with `openssl rand -hex 24`: hex is safe
    inside the database URL, while base64 may contain `/` or `+`.
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
    | `TABAYYUN_SESSION_SECRET` | a generated value, e.g. the output of `openssl rand -base64 48` |
    | `TABAYYUN_CACHE_URL` | `/data/cache` (the image default; the older name `TABAYYUN_CACHE_DIR` still works) |
    | `TABAYYUN_TIMESCALE` | optional; `auto` (default) uses TimescaleDB when the extension exists, `off` never does |

  - The image runs the schema migration (`python -m tabayyun.db.migrate upgrade head`) on every start before
    serving, so a redeploy upgrades the database in place; the app log shows the revision
    and `GET /api/version` reports it as `schema_revision`.
  - Persistent directory: `/data/cache`, label `tabayyun-cache`
  - Container HTTP port: `8000`
- HTTP Settings: no public domain needed (the web app proxies to it). If you want the API
  reachable directly, enable HTTPS on its default domain.
- Deployment tab → **Enable App Token**, copy it (used by CI below). For the first deploy,
  *Deploy via ImageName*: `ghcr.io/sira-labs/tabayyun-api:latest`.

## 3. Worker app: `tabayyun-worker`

Runs are executed by a worker process, not by the API. Create app `tabayyun-worker` with the
api image and one extra variable. The worker writes every successful upload to the Parquet
cache (spec 006): on a local directory by default, or on S3-compatible storage (section 3a),
which the live system uses.

| Name | Value |
|---|---|
| `TABAYYUN_ROLE` | `worker` |
| the api variables | identical to the api app (`TABAYYUN_ENV`, `TABAYYUN_DATABASE_URL`, `TABAYYUN_SESSION_SECRET`) |
| `TABAYYUN_CACHE_URL` and `TABAYYUN_S3_*` | see section 3a; without them the cache is `/data/cache` inside the container (tick persistent data with that path to keep it) |
| `TABAYYUN_WORKER_CONCURRENCY` | optional, default `2` |

- No HTTP settings: the worker serves nothing. Leave the container port at its default and
  do not enable HTTPS or connect a domain.
- Deployment tab → **Enable App Token**, copy it into the GitHub secret
  `CAPROVER_APP_TOKEN_WORKER`. The release workflow deploys the worker only when that secret
  exists, so nothing breaks before the app is created. First deploy via ImageName:
  `ghcr.io/sira-labs/tabayyun-api:latest`.
- The worker exits with code 3 until the api has migrated the schema to the same revision;
  CapRover restarts it. Its log shows `worker.start` with the revision once it runs.

## 3a. Cache store: RustFS

The Parquet cache lives on S3-compatible storage so later work (charts, several workers) can
share it. The live system runs RustFS (Apache-2.0); MinIO community builds ended in 2025.

1. One-time app `rustfs`: *Deploy via ImageName* `rustfs/rustfs:1.0.0` (pin the exact tag;
   upgrade deliberately), persistent directory `/data`, environment `RUSTFS_ACCESS_KEY` and
   `RUSTFS_SECRET_KEY` (strong root credentials), container HTTP port `9000`. No public
   domain for the S3 API; open the console (port 9001) only temporarily or over an SSH tunnel.
2. In the console: create bucket `tabayyun-cache` and an access key limited to it:

   ```json
   {"Version": "2012-10-17", "Statement": [
     {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": ["arn:aws:s3:::tabayyun-cache/*"]},
     {"Effect": "Allow", "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
      "Resource": ["arn:aws:s3:::tabayyun-cache"]}]}
   ```
3. On `tabayyun-worker` (and later the api, when charts read the cache):

   | Name | Value |
   |---|---|
   | `TABAYYUN_CACHE_URL` | `s3://tabayyun-cache` |
   | `TABAYYUN_S3_ENDPOINT` | `http://srv-captain--rustfs:9000` (internal network, S3 port 9000) |
   | `TABAYYUN_S3_ALLOW_HTTP` | `true` |
   | `TABAYYUN_S3_ACCESS_KEY_ID` / `TABAYYUN_S3_SECRET_ACCESS_KEY` | the limited key from step 2 |

4. Check: upload a CSV on `/runs/new`; the run's `stats.cache` shows `"written": true` and the
   bucket gains `raw/<source id>/<bucket>/<yyyy>/<mm>/part-*.parquet`. A store problem never
   fails a run: `stats.cache` then carries `"written": false` and the error, and the worker
   logs `cache.write_failed`.

## 4. Web app: `tabayyun-web`

- Create app `tabayyun-web` (no persistent data).
- App Configs → Environment variables: `TABAYYUN_API_UPSTREAM=srv-captain--tabayyun-api:8000`
  (Caddy inside the image proxies `/api` and `/healthz` there; `TABAYYUN_DOMAIN` stays
  unset so Caddy serves plain HTTP on :80 behind CapRover).
- Container HTTP port: `80`.
- HTTP Settings: connect your domain (e.g. `tabayyun.example.com`), Enable HTTPS, Force HTTPS.
- Deployment tab → **Enable App Token**, copy it. First deploy via ImageName:
  `ghcr.io/sira-labs/tabayyun-web:latest`.

Open the domain: the page should show the API version and let you upload a CSV.

## 5. Continuous deployment from GitHub Actions

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
| variable | `CAPROVER_APP_WORKER` | `tabayyun-worker` (optional, default) |
| secret | `CAPROVER_APP_TOKEN_WORKER` | app token from the worker app; the worker deploy step is skipped while it is unset |

The job is skipped until `CAPROVER_SERVER` exists. Images are public on GHCR, so CapRover
needs no registry credentials; if the repository ever becomes private, add the registry
under CapRover → Cluster → Docker Registries first.

## 6. Alternative: let CapRover build from GitHub (Method 3)

Instead of pulling images from GHCR, each app can clone the repository and build its own
image on the server. Both Dockerfiles use the repository root as build context, so the
`captain-definition` files under `deploy/caprover/` point at them:

| App | Deployment tab → captain-definition Relative Path |
|---|---|
| `tabayyun-api` | `./deploy/caprover/api/captain-definition` |
| `tabayyun-web` | `./deploy/caprover/web/captain-definition` |

In each app's Deployment tab fill in *Repository* (`github.com/Sira-Labs/Tabayyun`),
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

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `deploy-caprover` job fails with an HTML `404 Not Found` from nginx | `CAPROVER_SERVER` points at an app domain instead of the dashboard | Set it to `https://captain.<root-domain>` |
| API log: `db.schema_mismatch` and the container exits with code 3 | the database is at another migration revision than the image (an older image after a newer one migrated, or a migration that failed) | Redeploy the newest image; it migrates on start. Never run two api versions against one database |
| API log: `refusing to start in prod: ... ['TABAYYUN_DATABASE_URL']` | password is `tabayyun`, `changeme` or similar; prod rejects placeholders | Use a generated password in both the db app and the URL |
| API log: same message naming `TABAYYUN_SESSION_SECRET` | secret missing, shorter than 16 characters or a placeholder | Generate one and Save & Update |
| Web log: `dial tcp: lookup api ... no such host` | `TABAYYUN_API_UPSTREAM` missing or misspelled on the **web** app | Set it to `srv-captain--tabayyun-api:8000` (two dashes) and Save & Update |
| Web log: `lookup srv-captain--... no such host` | the API app has a different name | Match the upstream to `srv-captain--<api app name>:8000` |
| Run `stats.cache.error` ends in `failed to lookup address` or `Connection refused` | wrong `TABAYYUN_S3_ENDPOINT` (a placeholder, a missing `:9000`, or the app name) | Use `http://srv-captain--<store app>:9000`; `docker service ls` shows the name |
| Run `stats.cache.error` says `access denied` | a wrong key, or its policy does not cover the bucket | The policy needs object actions on `bucket/*` **and** `s3:ListBucket` on the bucket itself |
| Run `stats.cache.error` ends in `URL scheme is not allowed` | `http://` endpoint without `TABAYYUN_S3_ALLOW_HTTP=true` | Set it (internal endpoints only) |
| Run `stats.cache.error` says `not found (does the bucket exist?)` | the bucket in `TABAYYUN_CACHE_URL` does not exist | Create it in the store's console, or fix the name |
| An AWS-SDK tool (pyarrow, boto3, `aws s3`) uploading to an old MinIO fails with `411 MissingContentLength` | pre-2025 MinIO rejects the streamed checksums that newer AWS SDKs send | Tabayyun is unaffected; for the tool set `AWS_REQUEST_CHECKSUM_CALCULATION=WHEN_REQUIRED` and `AWS_RESPONSE_CHECKSUM_VALIDATION=WHEN_REQUIRED`, or move to RustFS |
| DB log: `superuser password is not specified` | image deployed before the env vars were saved | Save & Update the db app; it initialises on the next start |

Environment variable changes only take effect after **Save & Update** on that app's App Configs tab.

## Notes

- Keep the Hetzner firewall closed except 80/443 (CapRover) and your SSH port.
- Backups: CapRover persistent volumes live under `/captain/data/`; snapshot the server or
  `pg_dump` from a one-off container on the same network.
- Resource guidance for a pilot: 2 vCPU / 4 GB is enough for the API and web; give the
  database its own volume and watch disk for the Parquet cache.
