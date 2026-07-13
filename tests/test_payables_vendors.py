"""Payables vendors: person-or-org VendorProfile, inline-create org (+ Vendor category), defaults,
list/search/filter, party search fragment, soft-delete, and the exactly-one-party constraint."""

import pytest
from django.db import IntegrityError
from django_tenants.utils import schema_context

from apps.contacts.models import Person
from apps.organizations.models import Organization
from apps.payables.models import PaymentTerm, VendorProfile
from apps.tenants.models import Membership, Role


def _member(make_tenant, make_user, name="Acme HH", email="m@example.com"):
    tenant = make_tenant(name=name)
    user = make_user(email)
    Membership.objects.create(user=user, tenant=tenant, role=Role.MEMBER)
    return tenant, user


def _u(tenant, path=""):
    return f"/t/{tenant.schema_name}/payables/{path}"


def test_inline_create_org_vendor_tags_category(make_tenant, make_user, client):
    tenant, user = _member(make_tenant, make_user)
    client.force_login(user)
    resp = client.post(_u(tenant, "vendors/new/"), {
        "new_vendor_name": "Acme Supplies", "party_kind": "", "party_id": "",
        "account_number": "AC-1", "is_active": "on", "notes": "",
    })
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        org = Organization.objects.get(name="Acme Supplies")
        assert org.categories.filter(kind="ORG", name="Vendor").exists()
        vp = VendorProfile.objects.get(organization=org)
        assert vp.account_number == "AC-1" and vp.party_kind == "organization"


def test_create_person_vendor_with_defaults(make_tenant, make_user, client):
    tenant, user = _member(make_tenant, make_user)
    client.force_login(user)
    with schema_context(tenant.schema_name):
        person = Person.objects.create(first_name="Bob", last_name="Handyman")
        term = PaymentTerm.objects.get(name="Net 30")
    resp = client.post(_u(tenant, "vendors/new/"), {
        "party_kind": "person", "party_id": str(person.pk),
        "default_terms": str(term.pk), "account_number": "", "is_active": "on", "notes": "trusted",
    })
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        vp = VendorProfile.objects.get(person=person)
        assert vp.party_kind == "person" and vp.default_terms_id == term.pk
        assert vp.notes == "trusted"


def test_vendor_edit_defaults(make_tenant, make_user, client):
    tenant, user = _member(make_tenant, make_user)
    client.force_login(user)
    with schema_context(tenant.schema_name):
        org = Organization.objects.create(name="Utility Co")
        vp = VendorProfile.objects.create(organization=org)
        term = PaymentTerm.objects.get(name="Net 15")
    resp = client.post(_u(tenant, f"vendors/{vp.pk}/edit/"), {
        "default_terms": str(term.pk), "account_number": "UC-9", "is_active": "on", "notes": "",
    })
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        vp.refresh_from_db()
        assert vp.default_terms_id == term.pk and vp.account_number == "UC-9"


def test_vendor_list_search_and_filter(make_tenant, make_user, client):
    tenant, user = _member(make_tenant, make_user)
    client.force_login(user)
    with schema_context(tenant.schema_name):
        VendorProfile.objects.create(organization=Organization.objects.create(name="Zeta Traders"))
        VendorProfile.objects.create(
            person=Person.objects.create(first_name="Alice", last_name="Freelance")
        )
    assert b"Zeta Traders" in client.get(_u(tenant, "vendors/?q=zeta")).content
    orgs_only = client.get(_u(tenant, "vendors/?type=organization")).content
    assert b"Zeta Traders" in orgs_only and b"Alice" not in orgs_only
    people_only = client.get(_u(tenant, "vendors/?type=person")).content
    assert b"Alice" in people_only and b"Zeta" not in people_only


def test_vendor_search_fragment(make_tenant, make_user, client):
    tenant, user = _member(make_tenant, make_user)
    client.force_login(user)
    with schema_context(tenant.schema_name):
        Organization.objects.create(name="Searchable Org")
        Person.objects.create(first_name="Searchable", last_name="Person")
    r = client.get(_u(tenant, "vendor-search/?q=searchable"))
    assert r.status_code == 200
    assert b"Searchable Org" in r.content and b"Searchable Person" in r.content


def test_vendor_delete_keeps_contact(make_tenant, make_user, client):
    tenant, user = _member(make_tenant, make_user)
    client.force_login(user)
    with schema_context(tenant.schema_name):
        org = Organization.objects.create(name="Temp Vendor")
        vp = VendorProfile.objects.create(organization=org)
    assert client.post(_u(tenant, f"vendors/{vp.pk}/delete/")).status_code == 302
    with schema_context(tenant.schema_name):
        assert not VendorProfile.objects.filter(pk=vp.pk).exists()  # soft-deleted
        assert Organization.objects.filter(pk=org.pk).exists()      # contact kept


def test_one_party_constraint(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        org = Organization.objects.create(name="Both Co")
        person = Person.objects.create(first_name="Both", last_name="Person")
        with pytest.raises(IntegrityError):
            VendorProfile.objects.create(person=person, organization=org)
