"""Provisioning seeds the §6 system catalogs into each new tenant schema."""

from django_tenants.utils import schema_context

from apps.relationships.models import PersonOrgRelationshipType, RelationshipType
from apps.relationships.seed import P2O_TYPES, P2P_TYPES
from apps.setup.models import Category
from apps.setup.seed import ORG_CATEGORIES, PERSON_CATEGORIES


def test_new_tenant_is_seeded_with_locked_system_catalogs(make_tenant):
    tenant = make_tenant(name="Acme")

    with schema_context(tenant.schema_name):
        org = Category.objects.filter(kind=Category.Kind.ORG, is_system=True)
        person = Category.objects.filter(kind=Category.Kind.PERSON, is_system=True)
        assert org.count() == len(ORG_CATEGORIES)
        assert person.count() == len(PERSON_CATEGORIES)

        assert RelationshipType.objects.filter(is_system=True).count() == len(P2P_TYPES)
        assert PersonOrgRelationshipType.objects.filter(is_system=True).count() == len(P2O_TYPES)

        # Spot-check the seams future modules rely on (§9): "Bank"/"Vendor" categories + P2P labels.
        assert Category.objects.get(kind="ORG", name="Bank").is_system
        assert Category.objects.get(kind="ORG", name="Vendor").is_system  # Payables (module 6)
        parent = RelationshipType.objects.get(code="parent_child")
        assert (parent.a_label_m, parent.b_label_f) == ("Father", "Daughter")


def test_seeding_is_idempotent(make_tenant):
    tenant = make_tenant(name="Acme")
    from apps.setup.seed import seed_categories

    with schema_context(tenant.schema_name):
        before = Category.objects.count()
        seed_categories()  # re-run
        assert Category.objects.count() == before
