"""ContactChannel + Address: exactly-one-owner CHECK constraint and display helpers."""

import pytest
from django.db import IntegrityError, transaction
from django_tenants.utils import schema_context

from apps.contacts.models import Address, ContactChannel, Person
from apps.families.models import Family


def test_channel_requires_an_owner(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name), pytest.raises(IntegrityError):
        with transaction.atomic():
            ContactChannel.objects.create(type="phone", value="+91 1", person=None)


def test_channel_with_person_ok(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        p = Person.objects.create(first_name="A", last_name="B")
        ch = ContactChannel.objects.create(
            type="phone", value="+91 98450 12345", person=p, is_primary=True
        )
        assert p.channels.count() == 1
        assert ch.icon == "phone"
        assert p.primary_channel == ch


def test_address_requires_an_owner(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name), pytest.raises(IntegrityError):
        with transaction.atomic():
            Address.objects.create(city="Bengaluru", person=None)


def test_address_helpers(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        p = Person.objects.create(first_name="A", last_name="B")
        a = Address.objects.create(
            person=p, line1="42, Brigade Gardens", city="Bengaluru",
            region="Karnataka", postal_code="560001", country="India", is_primary=True,
        )
        assert "Bengaluru" in a.one_line
        assert a.locality == "Bengaluru, Karnataka 560001 · India"
        assert p.primary_city == "Bengaluru"


def test_family_can_own_contact_info(make_tenant):
    """P5 widened the owner to person | family — a family-owned row satisfies the CHECK."""
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        fam = Family.objects.create(name="Sharma")
        a = Address.objects.create(family=fam, city="Bengaluru")
        ch = ContactChannel.objects.create(type="phone", value="+91 1", family=fam)
        assert fam.addresses.count() == 1
        assert fam.channels.count() == 1
        assert a.family_id == fam.pk and ch.person_id is None


def test_two_owners_rejected(make_tenant):
    """Exactly-one-owner: a row naming both person and family violates the CHECK."""
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        p = Person.objects.create(first_name="A", last_name="B")
        fam = Family.objects.create(name="Sharma")
        with pytest.raises(IntegrityError), transaction.atomic():
            Address.objects.create(city="X", person=p, family=fam)
