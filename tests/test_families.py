"""Families: model + membership CRUD, (family, person) uniqueness, family detail listing only
inter-member relationships, family-owned Address, and soft-delete/restore."""

import pytest
from django.db import IntegrityError, transaction
from django.test import override_settings
from django_tenants.utils import schema_context

from apps.contacts.models import Address, Person
from apps.families.models import Family, FamilyMembership
from apps.relationships.models import PersonRelationship, RelationshipType
from apps.tenants.models import Membership, Role


def _owner(make_tenant, make_user, name="Acme", email="owner@example.com"):
    tenant = make_tenant(name=name)
    owner = make_user(email)
    Membership.objects.create(user=owner, tenant=tenant, role=Role.OWNER)
    return tenant, owner


def _c(tenant, path=""):
    return f"/t/{tenant.schema_name}/contacts/{path}"


def test_family_membership_unique(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        fam = Family.objects.create(name="Sharma")
        p = Person.objects.create(first_name="Rajesh", last_name="Sharma")
        FamilyMembership.objects.create(family=fam, person=p)
        with pytest.raises(IntegrityError), transaction.atomic():
            FamilyMembership.objects.create(family=fam, person=p)


def test_family_create_and_membership_via_views(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    assert client.post(_c(tenant, "families/new/"), {"name": "Sharma Household"}).status_code == 302
    with schema_context(tenant.schema_name):
        fam = Family.objects.get(name="Sharma Household")
        raj = Person.objects.create(first_name="Rajesh", last_name="Sharma", gender="M")
        priya = Person.objects.create(first_name="Priya", last_name="Sharma", gender="F")
        fid, rajid, priid = fam.pk, raj.pk, priya.pk

    client.post(_c(tenant, f"families/{fid}/members/add/"), {"person": rajid})
    client.post(_c(tenant, f"families/{fid}/members/add/"), {"person": priid})
    client.post(_c(tenant, f"families/{fid}/members/add/"), {"person": rajid})  # duplicate ignored
    with schema_context(tenant.schema_name):
        assert Family.objects.get(pk=fid).member_count == 2

    body = client.get(_c(tenant, f"families/{fid}/")).content.decode()
    assert "Rajesh Sharma" in body and "Priya Sharma" in body

    # Remove a member — the person survives, only the membership goes.
    client.post(_c(tenant, f"families/{fid}/members/{rajid}/remove/"))
    with schema_context(tenant.schema_name):
        assert Family.objects.get(pk=fid).member_count == 1
        assert Person.objects.filter(pk=rajid).exists()


def test_family_detail_lists_only_inter_member_edges(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    with schema_context(tenant.schema_name):
        fam = Family.objects.create(name="Sharma")
        raj = Person.objects.create(first_name="Rajesh", last_name="Sharma", gender="M")
        priya = Person.objects.create(first_name="Priya", last_name="Sharma", gender="F")
        outsider = Person.objects.create(first_name="Meera", last_name="Nair", gender="F")
        FamilyMembership.objects.create(family=fam, person=raj)
        FamilyMembership.objects.create(family=fam, person=priya)
        spouse = RelationshipType.objects.get(code="spouse")
        friend = RelationshipType.objects.get(code="friend")
        # Inter-member edge (both in the family) + an edge to an outsider (must not appear).
        PersonRelationship.objects.create(person_a=raj, person_b=priya, type=spouse)
        PersonRelationship.objects.create(person_a=raj, person_b=outsider, type=friend)
        fid = fam.pk

    body = client.get(_c(tenant, f"families/{fid}/")).content.decode()
    assert "Spouse" in body            # inter-member relationship surfaced
    assert "Meera Nair" not in body    # edge to a non-member is excluded


def test_family_owned_address(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    with schema_context(tenant.schema_name):
        fam = Family.objects.create(name="Sharma")
        fid = fam.pk
    client.post(_c(tenant, f"families/{fid}/addresses/new/"),
                {"label": "Home", "city": "Bengaluru", "is_primary": "on"})
    with schema_context(tenant.schema_name):
        addr = Address.objects.get(family_id=fid)
        assert addr.city == "Bengaluru" and addr.person_id is None


def test_family_soft_delete_restore(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    with schema_context(tenant.schema_name):
        fam = Family.objects.create(name="Gone Circle")
        fid = fam.pk
    assert client.post(_c(tenant, f"families/{fid}/delete/")).status_code == 302
    with schema_context(tenant.schema_name):
        assert Family.objects.filter(pk=fid).count() == 0
        assert Family.all_objects.get(pk=fid).is_deleted

    rd = f"/t/{tenant.schema_name}/setup/recently-deleted/"
    assert "Gone Circle" in client.get(rd).content.decode()
    assert client.post(rd + f"families/{fid}/restore/").status_code == 302
    with schema_context(tenant.schema_name):
        assert Family.objects.filter(pk=fid).exists()


@override_settings(ALLOW_HARD_DELETE=True)
def test_family_hard_delete_when_allowed(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    with schema_context(tenant.schema_name):
        fam = Family.objects.create(name="Zap Circle")
        fam.delete()
        fid = fam.pk
    client.post(f"/t/{tenant.schema_name}/setup/recently-deleted/families/{fid}/delete/")
    with schema_context(tenant.schema_name):
        assert not Family.all_objects.filter(pk=fid).exists()
