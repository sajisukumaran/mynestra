"""Tenancy substrate (public schema).

`Tenant` and `Domain` are the minimal django-tenants models required for schema-per-tenant
subfolder routing. Per DESIGN.md the URL slug **is** the schema name — so we keep a single source
of truth (`schema_name` from TenantMixin), expose it as `.slug`, and store the same value in
`Domain.domain` (which is what the subfolder middleware matches on).

Household-facing fields (palette, logo) and membership/invitation models arrive in P1.
"""

from django.db import models
from django_tenants.models import DomainMixin, TenantMixin


class Tenant(TenantMixin):
    # `schema_name` (max 63 chars, unique) is provided by TenantMixin and doubles as the slug.
    name = models.CharField(max_length=100)
    created_on = models.DateField(auto_now_add=True)

    # Create the PG schema (and migrate TENANT_APPS into it) on save; never auto-drop on delete.
    auto_create_schema = True
    auto_drop_schema = False

    @property
    def slug(self) -> str:
        """The /t/<slug>/ segment — identical to the schema name (DESIGN: 'slug = schema')."""
        return self.schema_name

    def __str__(self) -> str:
        return self.name


class Domain(DomainMixin):
    # DomainMixin provides `domain` (holds the slug for routing), `tenant` FK, and `is_primary`.
    pass
