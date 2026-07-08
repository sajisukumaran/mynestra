from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django_tenants.utils import get_public_schema_name

from apps.tenants.models import Domain, Tenant


@pytest.fixture(autouse=True)
def public_tenant(transactional_db):
    """Every request goes through TenantSubfolderMiddleware, which needs a public Tenant row.

    Also resets the connection to public at the start of each test: a prior test's client request
    to /t/<slug>/ leaves the connection pinned to that tenant schema (django-tenants doesn't reset
    it between test-client requests), which would break tenant creation in the next test.
    """
    connection.set_schema_to_public()
    tenant, _ = Tenant.objects.get_or_create(
        schema_name=get_public_schema_name(), defaults={"name": "Public"}
    )
    return tenant


@pytest.fixture
def make_user():
    User = get_user_model()

    def _make(email, password="pw-testing-12345", **extra):
        return User.objects.create_user(email=email, password=password, **extra)

    return _make


@pytest.fixture
def make_tenant():
    """Create a tenant (unique schema + Domain + §6 seed); drop the schema on teardown."""
    created = []

    def _make(name="Test Household", slug=None):
        connection.set_schema_to_public()  # tenants can only be created from the public schema
        slug = slug or f"t{uuid4().hex[:12]}"
        tenant = Tenant(schema_name=slug, name=name)
        tenant.save()  # CREATE SCHEMA + migrate + seed §6
        Domain.objects.get_or_create(domain=slug, tenant=tenant, defaults={"is_primary": True})
        created.append(tenant)
        return tenant

    yield _make

    for tenant in created:
        with connection.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA IF EXISTS "{tenant.schema_name}" CASCADE')
        Tenant.objects.filter(pk=tenant.pk).delete()
