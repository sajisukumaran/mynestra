"""Production settings — used by the deployed instance (behind the dockerlab-edge reverse proxy).

Prod-grade infrastructure config: gunicorn serves WSGI; WhiteNoise serves collected static from the
web process (the edge forwards /static/ here); Django serves /media/ (SERVE_MEDIA). HTTPS-only
hardening is gated behind SECURE_SSL (default off) because the edge currently forwards plain HTTP.
The deployed *test instance* runs THIS module with ENVIRONMENT=test in its .env (a label only —
see /health/); config.settings.test is the pytest module and is unrelated.
"""

from .base import *  # noqa: F401,F403

DEBUG = False

# SECRET_KEY and ALLOWED_HOSTS must be provided by the environment in production.
SECRET_KEY = env("SECRET_KEY")  # noqa: F405  — no default in prod
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS")  # noqa: F405

# Behind the edge proxy: trust its forwarded host + proto (TLS is a future cutover).
USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Django 5 checks the Origin header on unsafe requests against this list; without it every POST
# (login, invitations, forms) is rejected with 403. Include http + https for the eventual TLS move.
CSRF_TRUSTED_ORIGINS = env.list(  # noqa: F405
    "CSRF_TRUSTED_ORIGINS",
    default=["http://mynestra.dockerlab.test", "https://mynestra.dockerlab.test"],
)

# --- Static & media serving ------------------------------------------------------------------
# WhiteNoise serves collected static from the web process (the edge does NOT serve /static/). It
# sits immediately after SecurityMiddleware; TenantSubfolderMiddleware MUST remain first.
MIDDLEWARE = list(MIDDLEWARE)  # noqa: F405
MIDDLEWARE.insert(
    MIDDLEWARE.index("django.middleware.security.SecurityMiddleware") + 1,
    "whitenoise.middleware.WhiteNoiseMiddleware",
)

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# User-uploaded media (tenant logos, person/family photos) is served by Django in prod too: the
# edge forwards /media/ here and WhiteNoise handles only /static/ (see config/urls_public.py).
SERVE_MEDIA = True

# --- HTTPS hardening (opt-in) ----------------------------------------------------------------
# The edge currently forwards plain HTTP (X-Forwarded-Proto=http). Forcing secure cookies / an SSL
# redirect now would break the HTTP rollout, so gate them behind SECURE_SSL (flip on at TLS time).
SECURE_SSL = env.bool("SECURE_SSL", default=False)  # noqa: F405
if SECURE_SSL:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

# --- Logging ---------------------------------------------------------------------------------
# Django's default config routes 500 tracebacks to the (unconfigured) mail-admins handler and NOT
# to the console when DEBUG=False, so `docker compose logs web` stays silent on errors. Send
# everything to stderr so unhandled exceptions are visible in container logs.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "[{asctime}] {levelname} {name}: {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        # ERROR here carries the full traceback for unhandled 500s.
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
    },
}
