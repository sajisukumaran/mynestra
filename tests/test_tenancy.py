"""Tenant-isolation gate (standing). Now with real model data, not just raw schemas."""

from django.db import connection
from django_tenants.utils import schema_context

from apps.setup.models import Category


def _schema_exists(name: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", [name]
        )
        return cursor.fetchone() is not None


def test_two_tenants_get_isolated_schemas_and_data(make_tenant):
    a = make_tenant(name="Alpha")
    b = make_tenant(name="Beta")

    assert _schema_exists(a.schema_name)
    assert _schema_exists(b.schema_name)
    assert a.schema_name != b.schema_name

    # A row written in tenant A's schema must be invisible from tenant B (zero cross-schema leak).
    with schema_context(a.schema_name):
        baseline = Category.objects.count()
        Category.objects.create(kind=Category.Kind.ORG, name="Alpha-Only Bank", color="blue")
        assert Category.objects.count() == baseline + 1

    with schema_context(b.schema_name):
        assert not Category.objects.filter(name="Alpha-Only Bank").exists()
        assert Category.objects.count() == baseline  # same seeded baseline, no leak
