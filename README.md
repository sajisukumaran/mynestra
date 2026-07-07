# MyNestra

Personal multi-tenant household application. See [`docs/DESIGN.md`](docs/DESIGN.md) for the
authoritative design and [`docs/PROMPT.md`](docs/PROMPT.md) for the phased build plan. Approved UI
mockups live in [`docs/mockups/`](docs/mockups/).

## Stack

Django 5.2 · django-tenants (schema-per-tenant, subfolder routing `/t/<slug>/`) · PostgreSQL 17 ·
psycopg 3 · Tailwind CSS 4 (standalone CLI) · nginx · Mailpit. Python deps are managed with
[`uv`](https://docs.astral.sh/uv/); everything runs in Docker.

## Dev quickstart

Prerequisites: Docker Desktop, and (for generating the lockfile / running tools on the host) `uv`.

```powershell
cp .env.example .env        # optional; compose has sane defaults
uv lock                     # generate/update uv.lock (first time only)
./dev.ps1 build
./dev.ps1 up
```

Then open:

- App (via nginx): <http://localhost:8080/health/>
- Mailpit (email UI): <http://localhost:8025>

The `web` container waits for the DB, runs `migrate_schemas --shared`, ensures the public tenant,
then serves. The `tailwind` container compiles `static/css/tailwind.build.css` on change.

## Common tasks

```powershell
./dev.ps1 test              # pytest in the web container (incl. the tenant-isolation test)
./dev.ps1 migrate --shared  # migrate the public schema
./dev.ps1 makemigrations    # create migrations
./dev.ps1 shell             # Django shell
./dev.ps1 logs              # follow all logs
./dev.ps1 down              # stop the stack
```

`make <target>` mirrors these on POSIX shells.

## Layout

```
config/     Django project (settings split, urlconfs, wsgi/asgi)
apps/       tenants (Tenant/Domain), users (custom User), core (health)  + feature apps later
assets/     Tailwind input CSS
templates/  project templates
docker/     web / tailwind / nginx build + config
tests/      pytest suite
```
