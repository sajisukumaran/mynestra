# Deploying MyNestra (Jenkins → dockerlab-edge)

MyNestra deploys as a two-container stack (`web` + `db`) behind the shared **dockerlab-edge** nginx,
reachable at **http://mynestra.dockerlab.test**. Deploys are driven by a Jenkins Pipeline job using
[`Jenkinsfile`](../Jenkinsfile) + [`compose.prod.yaml`](../compose.prod.yaml). This is separate from
the local **dev** stack ([`compose.yaml`](../compose.yaml)), which is unchanged.

## Architecture

- The edge nginx is the only public proxy (on the external docker network `edge_network`). Its
  committed vhost forwards **everything** for `mynestra.dockerlab.test` — including `/static/` and
  `/media/` — to `http://mynestra-web:8000`, forwarding `Host` + `X-Forwarded-For` +
  `X-Forwarded-Proto` (currently `http`; TLS is a future cutover). `client_max_body_size 25m`,
  `proxy_read_timeout 120s`.
- `web`: gunicorn on `0.0.0.0:8000` (container `mynestra-web`). **WhiteNoise** serves `/static/`
  from the image (collected at build time); Django serves `/media/` from the `mynestra_media_files`
  volume. No published ports.
- `db`: `postgres:17` (container `mynestra-db`), on `edge_network` too so the edge's pgAdmin +
  nightly `pg_dump` reach it by name. Data in the `mynestra_pgdata` volume.
- Email: the shared edge `mailpit:1025`.

### Settings module
The deployed instance runs **`config.settings.prod`** (prod-grade: gunicorn, WhiteNoise, real SMTP,
secure password hashing, CSRF trusted origins). It is a **test instance**, so its `.env` sets
`ENVIRONMENT=test` — a display label only (shown on `/health/`); nothing branches on it.
(`config.settings.test` is the pytest module and is **not** used for deployment; local dev uses
`config.settings.dev`.)

### Static & media
- **Static** is built (Tailwind, minified) and `collectstatic`-ed into the image at build time and
  served by WhiteNoise (compressed + hashed manifest). There is deliberately **no staticfiles
  volume** — a named volume would shadow the baked files or serve stale assets after a rebuild.
  Every image rebuild ships fresh static.
- **Media** (tenant logos, person/family photos) is runtime-mutable, so it lives in the
  `mynestra_media_files` named volume and is served by Django (`SERVE_MEDIA=True` in prod).
- The edge caps uploads at 25 MiB (`client_max_body_size`). The app adds no separate cap; Django
  streams large uploads to disk.

## One-time host setup (on the edge/Jenkins host)

1. Ensure the external network exists (shared with the edge stack):
   ```sh
   docker network inspect edge_network >/dev/null 2>&1 || docker network create edge_network
   ```
2. Create the deploy dir and add the secrets file:
   ```sh
   mkdir -p /home/docker/deployments/mynestra
   # create /home/docker/deployments/mynestra/.env  (see the variables below)
   ```
   This dir is mounted into Jenkins at `/var/jenkins_home/deployments/mynestra` and is the job's
   workspace, so `docker compose` auto-loads that `.env`. **`.env` is never committed.**
3. Confirm the edge vhost for `mynestra.dockerlab.test` is in place (upstream `mynestra-web:8000`).
   Wildcard DNS for `*.dockerlab.test` already resolves.

## The `.env` (in the deploy dir — secrets, never committed)

```dotenv
DJANGO_SETTINGS_MODULE=config.settings.prod
DJANGO_DEBUG=false
ENVIRONMENT=test
SECRET_KEY=<generate a strong 50+ char key>          # e.g. python -c "import secrets;print(secrets.token_urlsafe(64))"
DJANGO_ALLOWED_HOSTS=mynestra.dockerlab.test,localhost,127.0.0.1
CSRF_TRUSTED_ORIGINS=http://mynestra.dockerlab.test,https://mynestra.dockerlab.test
POSTGRES_DB=mynestra
POSTGRES_USER=mynestra
POSTGRES_PASSWORD=<strong password>
POSTGRES_HOST=db
POSTGRES_PORT=5432
EMAIL_HOST=mailpit
EMAIL_PORT=1025
DEFAULT_FROM_EMAIL=MyNestra <no-reply@mynestra.dockerlab.test>
ALLOW_HARD_DELETE=0
TZ=America/New_York
# Optional — flip to true only after the edge serves TLS (adds secure cookies + SSL redirect):
# SECURE_SSL=false
```

## The Jenkins job

Create a **Pipeline** job → "Pipeline script from SCM" → Git
`https://github.com/sajisukumaran/mynestra`, branch `main`, Script Path `Jenkinsfile`.
**Do not enable "wipe out workspace / clean before checkout"** — it would delete the deploy `.env`.

Pipeline stages ([`Jenkinsfile`](../Jenkinsfile)): **Checkout** → **Build**
(`docker compose -f compose.prod.yaml build`) → **Deploy** (`… up -d`) → **Prune**
(`docker image prune -f`) → **Verify** (`… ps` + poll `/health/` inside the web container).
Database migrations run automatically via the image entrypoint on container start (waits for db →
`migrate_schemas --shared` → `ensure_public_tenant`).

## Post-deploy: create the first tenant + owner

A fresh prod DB has only the public tenant. Create a household + owner login with the shipped
management command:
```sh
cd /home/docker/deployments/mynestra
docker compose -f compose.prod.yaml exec web python manage.py bootstrap    # or: create_tenant
```
Then log in at `http://mynestra.dockerlab.test/` and open `/t/<slug>/`.

## Migrations note

The entrypoint runs `migrate_schemas --shared` on every start (shared/public apps). Brand-new
tenants are migrated when they're created. If you later add **tenant-app** migrations, existing
tenant schemas need a one-off:
```sh
docker compose -f compose.prod.yaml exec web python manage.py migrate_schemas
```

## Verifying / troubleshooting

- `docker compose -f compose.prod.yaml ps` — both healthy.
- `docker compose -f compose.prod.yaml exec web curl -fsS http://localhost:8000/health/` — JSON-ish
  200 with `environment: test` (503 means the DB is unreachable).
- `curl -I http://mynestra.dockerlab.test/static/css/tailwind.build.css` — 200 via WhiteNoise.
- Logs: `docker compose -f compose.prod.yaml logs --tail=100 web`.
