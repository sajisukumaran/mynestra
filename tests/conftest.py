import pytest
from django_tenants.utils import get_public_schema_name, get_tenant_model


@pytest.fixture
def public_tenant(transactional_db):
    """Ensure the public-schema Tenant row exists (the subfolder middleware needs it)."""
    Tenant = get_tenant_model()
    tenant, _ = Tenant.objects.get_or_create(
        schema_name=get_public_schema_name(),
        defaults={"name": "Public"},
    )
    return tenant
