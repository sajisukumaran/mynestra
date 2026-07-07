"""Production settings (minimal in P0; hardened at deploy time)."""

from .base import *  # noqa: F401,F403

DEBUG = False

# SECRET_KEY and ALLOWED_HOSTS must be provided by the environment in production.
SECRET_KEY = env("SECRET_KEY")  # noqa: F405  — no default in prod
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS")  # noqa: F405

USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
