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
    "apps.relationships",      # P2P/P2O relationship-type catalogs + P2P/P2O edges (P5/P6)
    "apps.contacts",           # People, contact channels/addresses, important dates (P4)
    "apps.families",           # Families + membership (P5); owns Address via unified FK
    "apps.organizations",      # Organizations + identifiers + branches (P6)
    "apps.finance",            # Double-entry GL backbone: COA, currencies, ledger (module 2)
    "apps.banking",            # Bank accounts + transaction register; posts to the GL (module 3)
    "apps.cards",              # Credit cards (liability ledger) + debit-card registry (module 4)
    "apps.investments",        # Investment accounts, holdings + tax lots → GL (module 5)
    "apps.payables",           # Vendor bills + payments + item catalog; posts to the GL (module 6)
    "apps.loans",              # Loans & liabilities: debt register + amortization → GL (module 7)
    "apps.automobile",         # Vehicles: owned-at-cost + leased; costs → locked bills (module 8)
    "apps.insurance",          # Insurance: policies + premiums (→ locked bills) + claims (Plan B)
    "apps.realestate",         # Real Estate: owned property at cost; costs → locked bills (Plan C)
    "apps.health",             # Health: visits + provider invoices (→ locked bills) + Rx (Plan D)
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

# --- Investments: end-of-day price auto-fetch ----------------------------
# The `fetch_eod_prices` command pulls daily closes for auto-tracked securities. Provider is
# swappable (stooq = keyless default; alphavantage / finnhub = keyed; yfinance = keyless library).
# Keyed providers read PRICE_API_KEY. Manual price entry always works alongside this.
INVESTMENTS_PRICE_PROVIDER = env("PRICE_PROVIDER", default="stooq")
INVESTMENTS_PRICE_API_KEY = env("PRICE_API_KEY", default="")
