# MyNestra web (Django). Dev image: source is bind-mounted at runtime; deps live in /opt/venv
# (outside /app) so the mount never shadows them.
FROM python:3.12-slim

# uv, pinned, copied from the official distroless image (no pip/venv bootstrapping).
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# postgresql-client provides pg_isready for the entrypoint wait-for-db loop.
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first for layer caching. --frozen requires an up-to-date uv.lock.
# Dev group is included (pytest runs inside this container in the dev stack).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY docker/web-entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
