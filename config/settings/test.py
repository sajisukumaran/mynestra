"""Test settings — fast, deterministic, points at the compose db by env."""

from .base import *  # noqa: F401,F403

DEBUG = False

# Fast password hashing in tests.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Silence noisy migration output isn't needed here; keep behaviour close to real.
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
