#!/usr/bin/env sh
set -e

DB_HOST="${POSTGRES_HOST:-db}"
DB_PORT="${POSTGRES_PORT:-5432}"
DB_USER="${POSTGRES_USER:-mynestra}"

echo "Waiting for postgres at ${DB_HOST}:${DB_PORT} ..."
until pg_isready -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" >/dev/null 2>&1; do
  sleep 1
done
echo "Postgres is up."

echo "Applying shared migrations (migrate_schemas --shared) ..."
python manage.py migrate_schemas --shared --noinput

echo "Ensuring public tenant exists ..."
python manage.py ensure_public_tenant

# Apply TENANT_APPS migrations to every existing tenant schema. `--shared` above only touches the
# public schema, so without this a newly added tenant-app migration (e.g. a new column) breaks
# existing tenants at request time until migrated by hand. Idempotent — a no-op when up to date.
echo "Applying tenant migrations (migrate_schemas --tenant) ..."
python manage.py migrate_schemas --tenant --noinput

exec "$@"
