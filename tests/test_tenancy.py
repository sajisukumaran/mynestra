"""Tenant-isolation harness (standing gate).

P0 asserts schema-level separation: two tenants get two distinct PostgreSQL schemas, and an object
created in one schema is not visible from the other's search_path. The full model-level
"write in A, invisible in B" assertion extends this in P1 (on the first seeded tenant catalogs).
"""

from django.db import connection
from django_tenants.utils import get_tenant_domain_model, get_tenant_model, schema_context


def _schema_exists(name: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", [name]
        )
        return cursor.fetchone() is not None


def test_two_tenants_get_isolated_schemas(transactional_db):
    Tenant = get_tenant_model()
    Domain = get_tenant_domain_model()

    for slug in ("alpha", "beta"):
        tenant = Tenant(schema_name=slug, name=slug.title())
        tenant.save()  # auto_create_schema -> CREATE SCHEMA + migrate TENANT_APPS
        Domain(domain=slug, tenant=tenant, is_primary=True).save()
        assert tenant.slug == slug  # DESIGN: slug == schema_name

    assert _schema_exists("alpha")
    assert _schema_exists("beta")

    # Cross-schema isolation: a probe table in alpha is invisible from beta's search_path.
    with schema_context("alpha"):
        assert connection.schema_name == "alpha"
        with connection.cursor() as cursor:
            cursor.execute("CREATE TABLE IF NOT EXISTS iso_probe (id integer)")

    with schema_context("beta"):
        assert connection.schema_name == "beta"
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('iso_probe')")
            assert cursor.fetchone()[0] is None  # not on beta's search_path -> isolated

    with schema_context("alpha"):
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('iso_probe')")
            assert cursor.fetchone()[0] is not None  # present in its own schema
