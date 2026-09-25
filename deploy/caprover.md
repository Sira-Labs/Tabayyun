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

## Staging and production (ADR-0016)

Two independent CapRover servers at Hetzner, shared with Arqam and Suffa (Sīra family,
Arqam ADR-0020):

| Server | Apps | Domain | Data |
|---|---|---|---|
| **Staging and tools** (the current server) | `tabayyun-db-stg`, `tabayyun-api-stg`, `tabayyun-worker-stg`, `tabayyun-web-stg`; GlitchTip and uptime checks for both servers | `tabayyun-stg.siralabs.org` | test data only |
| **Production** (new server in Germany) | `tabayyun-db`, `tabayyun-api`, `tabayyun-worker`, `tabayyun-web`, its own `rustfs`, and Keycloak once it serves real users | `tabayyun.siralabs.org` | real people's data, and only there |

Sections 1–4 below set up one server's apps with the production names. On staging every app
name gets the `-stg` suffix, and so does every internal address that names an app:

| Setting | Production | Staging |
|---|---|---|
| `TABAYYUN_MIGRATION_DATABASE_URL`, `TABAYYUN_DATABASE_URL` host | `srv-captain--tabayyun-db` | `srv-captain--tabayyun-db-stg` |
| `TABAYYUN_API_UPSTREAM` (web app) | `srv-captain--tabayyun-api:8000` | `srv-captain--tabayyun-api-stg:8000` |
| `TABAYYUN_CACHE_URL` bucket | `s3://tabayyun-cache` | `s3://tabayyun-stg-cache` |
| Persistent directory label of the db | `tabayyun-pgdata` | `tabayyun-stg-pgdata` |

A staging app that keeps an unsuffixed address talks to the old apps on the same server.
`srv-captain--rustfs` resolves only inside one CapRover, so each server runs its own RustFS
and that endpoint name stays the same.

Rules:

- Personal data of real people lives only on production. People who used the current
  install sign up again on production; nothing is copied across.
- Staging and production have separate secrets: database passwords, session secret, S3 keys,
  OAuth clients and CapRover app tokens.
- `main` deploys to staging automatically; production runs the image digest staging runs,
  after the owner approves it (section 5).
- Production Postgres is backed up continuously (section 7); restore drills go into a
  throwaway database on the production server, never into staging.

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
- Check before the first real data: App Configs must show the persistent directory above.
  Without it the data lives in the container and a restart (any Save & Update) starts an
  empty database. CapRover cannot add persistent data to an existing app: delete and
  recreate it with the box ticked.
- `POSTGRES_USER`, `POSTGRES_PASSWORD` and `POSTGRES_DB` only apply when the data directory
  is created. Changing them later changes nothing in the database (the owner keeps its
  password), so leave them as they were; to change a password, `ALTER ROLE` in `psql`.

## 2. API app: `tabayyun-api`

- Create app `tabayyun-api` (persistent data ticked for the Parquet cache).
- App Configs:
  - Environment variables:

    | Name | Value |
    |---|---|
    | `TABAYYUN_ENV` | `prod` |
    | `TABAYYUN_MIGRATION_DATABASE_URL` | `postgresql+psycopg://tabayyun:<password>@srv-captain--tabayyun-db:5432/tabayyun` (the owner; migrations only) |
    | `TABAYYUN_DATABASE_URL` | `postgresql+psycopg://tabayyun_app:<app password>@srv-captain--tabayyun-db:5432/tabayyun` (the app login, see "Database logins" below) |
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
| the api variables | identical to the api app (`TABAYYUN_ENV`, `TABAYYUN_DATABASE_URL` with the app login, `TABAYYUN_SESSION_SECRET`); the worker never needs `TABAYYUN_MIGRATION_DATABASE_URL` |
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

   On staging, create bucket `tabayyun-stg-cache` and a separate key whose policy names
   `tabayyun-stg-cache` in both resources (the same JSON with the bucket name replaced), and
   use it on the `-stg` apps. Staging and production never share a key or a bucket.
4. Check: upload a CSV on `/runs/new`; the run's `stats.cache` shows `"written": true` and the
   bucket gains `raw/<source id>/<bucket>/<yyyy>/<mm>/part-*.parquet`. A store problem never
   fails a run: `stats.cache` then carries `"written": false` and the error, and the worker
   logs `cache.write_failed`.

## Database logins (spec 007)

Row-level security keeps each org's rows apart, and only works when the api and the worker
log in as `tabayyun_app` rather than the database superuser (`deploy/README.md`, "Database
logins and row-level security"). The api's migration step creates the `tabayyun_app` login
with the password from `TABAYYUN_DATABASE_URL`; nothing needs doing in the database itself.

Switching an existing install (once, **after** the release with migration 0004 is live:
`GET /api/version` shows `"schema_revision": "0004"`). Switching earlier locks the running
release out, because only 0004 creates the `tabayyun_app` login. Both apps must carry the
same app password, and `tabayyun-db` is not touched:

1. Generate the app password: `openssl rand -hex 24` (hex, because it sits in a URL).
2. `tabayyun-api` → App Configs: add `TABAYYUN_MIGRATION_DATABASE_URL` with the current
   `TABAYYUN_DATABASE_URL` value (the owner), then change `TABAYYUN_DATABASE_URL` to
   `postgresql+psycopg://tabayyun_app:<app password>@srv-captain--tabayyun-db:5432/tabayyun`.
   Save & Update: the api migrates as the owner, creates the login, then serves as it.
3. `tabayyun-worker` → App Configs: the same new `TABAYYUN_DATABASE_URL`. Save & Update.
4. Check: neither app logs `db.rls_bypassed` any more, and
   `SELECT usename FROM pg_stat_activity WHERE datname = 'tabayyun'` in the db app's
   terminal shows `tabayyun_app` for both.

Until the switch, both apps keep serving and log `db.rls_bypassed` as an error on start.

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

## 5. Continuous deployment: staging, then promotion to production

Two workflows (ADR-0016):

- **`release.yml`**: every push to `main` (and every `v*` tag on a commit on `main`) builds,
  scans and publishes the images, then its `deploy-staging` job deploys them to the `-stg`
  apps and waits until staging serves the commit.
- **`promote.yml`**: Actions → promote → Run workflow on `main`, with the commit staging
  serves (`GET https://tabayyun-stg.siralabs.org/api/version` → `commit`). It checks that the
  commit is on `main` and live on staging (web, api and worker), pins the images to their
  digests, waits for the owner's approval on the `production` environment, deploys those
  digests (`tag@sha256:…`) to the production apps and waits until production serves the
  commit.

`sha-<short>` tags are immutable: a re-run of `release.yml` for the same commit leaves them
on the digest staging got, and runs for the same commit are serialised so two cannot create
the tag at once. Promotion resolves that tag to digests and deploys by digest, on CapRover
(`tag@sha256:…`) and on a compose host (`TABAYYUN_API_IMAGE`, `TABAYYUN_WEB_IMAGE`).

Both wait-until-live checks are `.github/scripts/wait-live.sh`, and both fail when their
environment has no `CAPROVER_WEB_URL`: CapRover accepts a deploy
before it pulls the image, so without the check a failed pull or a container that never
starts would leave the run green. It polls `<url>/version.json` (web image) and
`<url>/api/version` (api image) for the commit, and waits for a connected worker on that
commit (the worker names its database connections `tabayyun-worker/<commit>`). That shows a
worker on the commit is connected, not that the old one has stopped.

### GitHub settings (owner, once)

Settings → Environments:

| Environment | Deployment branches and tags | Protection | Variables | Secrets |
|---|---|---|---|---|
| `staging` | Selected: branch `main`, tag pattern `v*` | none | `CAPROVER_SERVER` (the current server's `https://captain.…`), `CAPROVER_WEB_URL=https://tabayyun-stg.siralabs.org`; optional `CAPROVER_APP_API`, `_WEB`, `_WORKER` (default `tabayyun-*-stg`) | `CAPROVER_APP_TOKEN_API`, `_WEB`, `_WORKER` of the `-stg` apps |
| `production` | Selected: branch `main` | Required reviewer: the owner; prevent self-review off | `CAPROVER_SERVER` (the production server), `CAPROVER_WEB_URL=https://tabayyun.siralabs.org`; for a compose host instead: `DEPLOY_HOST`, `DEPLOY_USER` | `CAPROVER_APP_TOKEN_API`, `_WEB`, `_WORKER` of the production apps; `DEPLOY_SSH_KEY` for a compose host |

Then delete the repository-level `CAPROVER_*` variables and secrets (Settings → Secrets and
variables → Actions): repository values reach every job, environment values only the jobs
that bind the environment and pass its rules. The rules hold even if a branch edits a
workflow file: a job that names `production` from any branch other than `main` is refused
before it sees a secret.

Release tags: import `.github/rulesets/protect-release-tags.json` (CONTRIBUTING.md,
"Repository settings") so that only organisation admins can create, move or delete `v*`
tags.

### Move from one server to two (owner)

1. Create the `-stg` apps on the current server (sections 1–4 with the suffix; persistent
   data on `tabayyun-db-stg`), with new secrets and the `tabayyun-stg.siralabs.org` domain
   on `tabayyun-web-stg`. Set up the two environments above; the next push to `main`
   deploys staging.
2. Order the production server, install CapRover (strong dashboard password, 2FA, SSH by
   key only, firewall open for 80, 443 and 22), and create the production apps and RustFS
   with production secrets. Leave `tabayyun.siralabs.org` on the old server for now.
3. Set the `production` environment's `CAPROVER_WEB_URL` to the new web app's temporary
   CapRover address (`https://tabayyun-web.<new server's root domain>`) and run promote.yml
   for the commit staging runs; it verifies production there.
4. Point `tabayyun.siralabs.org` at the new server, connect it on `tabayyun-web` (Enable and
   Force HTTPS), set `CAPROVER_WEB_URL=https://tabayyun.siralabs.org` on `production`, and
   check `GET /api/version` on the public domain.
5. Remove the old install from the current server: delete the old `tabayyun-db`,
   `tabayyun-api`, `tabayyun-worker` and `tabayyun-web` apps together with their volumes
   (CapRover asks when deleting an app; afterwards `docker volume ls | grep tabayyun` should
   list only `-stg` volumes), and the old `tabayyun-cache` bucket and its objects from RustFS
   (RustFS itself stays: staging uses it). The data is test data, but it should not linger.

Images are public on GHCR, so neither server needs registry credentials; if the repository
ever becomes private, add the registry under CapRover → Cluster → Docker Registries first.

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
- No app tokens are needed; the `deploy-staging` job stays skipped as long as
  `CAPROVER_SERVER` is unset on the `staging` environment.

Do not enable both paths for the same app, or each push deploys it twice.

## 7. Production backups (ADR-0016)

Required before real users arrive on production; not built yet (TASKS.md, "Production server").
Staging needs none of it: its data can be rebuilt.

- **Point-in-time recovery for Postgres:** continuous WAL archiving with WAL-G or pgBackRest
  (both support TimescaleDB), on top of physical base backups: a full every week and a
  delta every day, at least two fulls kept. WAL replays only onto a base backup, so the
  recovery point of minutes rests on them.
- **Nightly logical dump:** `pg_dump -Fc` of `tabayyun`. Restoring a TimescaleDB dump needs
  `SELECT timescaledb_pre_restore();` before and `SELECT timescaledb_post_restore();` after.
- **The Parquet cache bucket** holds the only copy of uploaded series once their runs
  finished: copy it with versioning (deleted objects kept) along with the database backups.
- **Where:** everything encrypted (WAL-G libsodium or pgBackRest `repo-cipher`; the dump and
  bucket copies with `age` or `rclone crypt`), to S3-compatible object storage in a
  different Hetzner location than the production server. The encryption keys also go into
  the owner's password manager.
- **Restore drill:** restore the latest base backup plus WAL, and the latest dump, into a
  throwaway database on the production server, compare row counts and `GET /api/version`
  against it, time it, drop it, and record the time in `TASKS.md`. Never restore production
  data into staging.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `deploy-staging` or `promote` fails with an HTML `404 Not Found` from nginx | `CAPROVER_SERVER` points at an app domain instead of the dashboard | Set it to `https://captain.<root-domain>` on that environment |
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
