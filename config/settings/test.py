"""Test settings — fast, deterministic, points at the compose db by env."""

from .base import *  # noqa: F401,F403

DEBUG = False

# Per-session test database (avoids concurrent Claude/CI sessions clobbering one shared test DB).
# Django names the test DB `test_<POSTGRES_DB>` (= `test_mynestra`) by default; when TEST_DB_NAME is
# set, this run owns that DB instead. Django still uses the real `mynestra` DB as the maintenance
# connection to CREATE it, and the conftest `synchronous_commit` guard keys on the `test_` prefix.
# Convention: each session exports a UNIQUE name, e.g. TEST_DB_NAME=test_mynestra_<session-id>.
_test_db_name = env("TEST_DB_NAME", default=None)  # noqa: F405
if _test_db_name:
    DATABASES["default"]["TEST"] = {"NAME": _test_db_name}  # noqa: F405

# Fast password hashing in tests.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Silence noisy migration output isn't needed here; keep behaviour close to real.
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
