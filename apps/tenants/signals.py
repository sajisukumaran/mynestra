"""Auto-seed §6 system catalogs into every newly provisioned tenant schema.

`post_schema_sync` fires from `TenantMixin.save()` after CREATE SCHEMA + tenant migrations — but
the connection is back on the *public* schema at that point, so we must switch into the tenant's
schema to write. sender is the abstract `TenantMixin` (NOT the concrete Tenant), or the receiver
never fires. If this raises, django-tenants rolls back and drops the freshly created schema.
"""

from django.dispatch import receiver
from django_tenants.models import TenantMixin
from django_tenants.signals import post_schema_sync
from django_tenants.utils import get_public_schema_name, schema_context


@receiver(post_schema_sync, sender=TenantMixin)
def seed_new_tenant(sender, tenant, **kwargs):
    if tenant.schema_name == get_public_schema_name():
        return  # never seed the public schema

    # Imported lazily so app loading doesn't depend on import order.
    from apps.relationships.seed import seed_relationship_types
    from apps.setup.seed import seed_categories

    with schema_context(tenant.schema_name):
        seed_categories()
        seed_relationship_types()
