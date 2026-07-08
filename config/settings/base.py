"""Base settings shared by all environments.

MyNestra is multi-tenant (schema-per-tenant) via django-tenants with SUBFOLDER routing:
tenant URLs are `/t/<slug>/...` where the slug is stored in `Domain.domain` and equals the
PostgreSQL schema name. See docs/DESIGN.md §3.
"""

from pathlib import Path

import environ

# config/settings/base.py -> project root is three parents up.
BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
    ALLOW_HARD_DELETE=(bool, False),
)
# Local dev convenience: read a .env if present. In Docker, env comes from the container.
environ.Env.read_env(BASE_DIR / ".env")

# --- Core ----------------------------------------------------------------
SECRET_KEY = env("SECRET_KEY", default="dev-insecure-change-me")
DEBUG = env("DJANGO_DEBUG")
ENVIRONMENT = env("ENVIRONMENT", default="dev")
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["*"])
ALLOW_HARD_DELETE = env("ALLOW_HARD_DELETE")

# --- Applications: schema-per-tenant split (django-tenants) --------------
# SHARED_APPS live in the `public` schema (identity/tenancy). TENANT_APPS live in each tenant
# schema. django_tenants MUST be first. Per-tenant feature apps (contacts, organizations, ...)
# arrive in later phases.
SHARED_APPS = [
    "django_tenants",          # must be first
    "apps.tenants",            # Tenant + Domain + Membership + Invitation
    "apps.users",              # custom AUTH_USER_MODEL (shared identity)
    "apps.accounts",           # identity views: chooser, invitation accept/create (no models)
    "apps.core",               # health + middleware + shared views (no models)
    "django_cotton",           # HTML component library (auto-wires template loaders + builtins)
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

TENANT_APPS = [
    "simple_history",          # audit history for tenant models (DESIGN §3); no tables of its own
    "apps.setup",              # Category catalogs (seeded §6)
    "apps.relationships",      # P2P/P2O relationship-type catalogs (seeded §6)
    "apps.contacts",           # People, contact channels/addresses, important dates (P4)
    # organizations/families join in P5/P6.
]

INSTALLED_APPS = list(SHARED_APPS) + [a for a in TENANT_APPS if a not in SHARED_APPS]

TENANT_MODEL = "tenants.Tenant"
TENANT_DOMAIN_MODEL = "tenants.Domain"
TENANT_SUBFOLDER_PREFIX = "t"

# --- Middleware ----------------------------------------------------------
MIDDLEWARE = [
    "django_tenants.middleware.TenantSubfolderMiddleware",  # MUST be first
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.core.middleware.MembershipMiddleware",  # after auth; enforces per-tenant access
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "simple_history.middleware.HistoryRequestMiddleware",  # stamps the acting user on history rows
]

# Tenant routes are served under /t/<slug>/ from ROOT_URLCONF; every non-prefixed path
# (/health/, /login/, /invite/<token>/, /admin/) is served from PUBLIC_SCHEMA_URLCONF in the
# public schema. BOTH must be defined — without PUBLIC_SCHEMA_URLCONF, public paths silently fall
# through to the tenant urlconf.
ROOT_URLCONF = "config.urls_tenants"
PUBLIC_SCHEMA_URLCONF = "config.urls_public"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.ui",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# --- Database (django-tenants) -------------------------------------------
DATABASES = {
    "default": {
        "ENGINE": "django_tenants.postgresql_backend",
        "NAME": env("POSTGRES_DB", default="mynestra"),
        "USER": env("POSTGRES_USER", default="mynestra"),
        "PASSWORD": env("POSTGRES_PASSWORD", default="mynestra"),
        "HOST": env("POSTGRES_HOST", default="localhost"),
        "PORT": env.int("POSTGRES_PORT", default=5432),
    }
}
DATABASE_ROUTERS = ["django_tenants.routers.TenantSyncRouter"]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "users.User"

# --- Auth flow ------------------------------------------------------------
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"          # tenant chooser
LOGOUT_REDIRECT_URL = "/login/"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- I18N / TZ -----------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = env("TZ", default="UTC")
USE_I18N = True
USE_TZ = True

# --- Static --------------------------------------------------------------
# `static/` (STATICFILES_DIRS) is the source dir where the Tailwind watcher writes
# css/tailwind.build.css and where nginx serves /static/ from. collectstatic target is separate.
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

# --- Media (tenant logos, later avatars) ---------------------------------
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# --- Email (Mailpit in dev) ----------------------------------------------
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = env("EMAIL_HOST", default="localhost")
EMAIL_PORT = env.int("EMAIL_PORT", default=1025)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=False)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="MyNestra <no-reply@mynestra.local>")
