# MyNestra web — PRODUCTION image. Unlike the dev image, the source is baked in (no bind-mount),
# the Tailwind CSS is built one-shot + minified, static is collected at build time, and gunicorn
# serves WSGI. The dev image (docker/web.Dockerfile) is unchanged.

# --- Stage 1: build the Tailwind CSS (mirrors docker/tailwind.Dockerfile, one-shot + minified) ---
FROM debian:bookworm-slim AS tailwind

ARG TAILWIND_VERSION=v4.3.2

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -sL -o /usr/local/bin/tailwindcss \
        "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/tailwindcss-linux-x64" \
    && chmod +x /usr/local/bin/tailwindcss

WORKDIR /app
# Needs templates/ + apps/ + assets/css/app.css + static/ present so Tailwind can scan for classes.
COPY . .
RUN tailwindcss -i assets/css/app.css -o static/css/tailwind.build.css --minify

# --- Stage 2: the runtime image ---------------------------------------------------------------
FROM python:3.12-slim

# uv, pinned, copied from the official distroless image (no pip/venv bootstrapping).
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# postgresql-client -> pg_isready for the entrypoint wait-loop; curl -> the healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install PRODUCTION deps only (no dev group), cached on the dependency layer. --frozen needs an
# up-to-date uv.lock (gunicorn + whitenoise are locked in).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Bake the source in, then overwrite the Tailwind build with the minified one from stage 1.
COPY . /app
COPY --from=tailwind /app/static/css/tailwind.build.css static/css/tailwind.build.css

COPY docker/web-entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Collect static at build time (WhiteNoise serves it from the image). prod settings require
# SECRET_KEY / ALLOWED_HOSTS / DJANGO_DEBUG from env, so pass throwaway build-only values — none are
# used at runtime (real values come from the deploy .env). collectstatic does not touch the DB.
RUN DJANGO_SETTINGS_MODULE=config.settings.prod \
    DJANGO_DEBUG=false \
    SECRET_KEY=build-only-not-used-at-runtime \
    DJANGO_ALLOWED_HOSTS=localhost \
    python manage.py collectstatic --noinput

# /health/ returns 200 once the DB is reachable (SELECT 1), 503 otherwise.
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=5 \
    CMD curl -fsS http://localhost:8000/health/ || exit 1

# Entrypoint: wait for db -> migrate_schemas --shared -> ensure_public_tenant, then exec CMD.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "120"]
