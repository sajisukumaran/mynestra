"""Tenant-isolation gate (standing). Now with real model data, not just raw schemas."""

from django.db import connection
from django_tenants.utils import schema_context

from apps.contacts.models import Person
from apps.families.models import Family
from apps.relationships.models import PersonRelationship, RelationshipType
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


def test_families_and_relationships_are_isolated(make_tenant):
    """P5 models (Family, PersonRelationship) must not leak across tenant schemas."""
    a = make_tenant(name="Alpha")
    b = make_tenant(name="Beta")

    with schema_context(a.schema_name):
        Family.objects.create(name="Alpha-Only Family")
        pa = Person.objects.create(first_name="Alpha", last_name="A", gender="M")
        pb = Person.objects.create(first_name="Alpha", last_name="B", gender="F")
        PersonRelationship.objects.create(
            person_a=pa, person_b=pb, type=RelationshipType.objects.get(code="spouse")
        )
        assert Family.objects.count() == 1
        assert PersonRelationship.objects.count() == 1

    with schema_context(b.schema_name):
        assert Family.objects.count() == 0
        assert PersonRelationship.objects.count() == 0
        assert not Family.objects.filter(name="Alpha-Only Family").exists()
