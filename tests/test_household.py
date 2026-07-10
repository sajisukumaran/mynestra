"""Household-member flag on Person: the person-form toggle persists it, the People list filters on
it, and the service counts it."""

from django_tenants.utils import schema_context

from apps.contacts.models import Person
from apps.contacts.services import count_household_members
from apps.tenants.models import Membership, Role


def _owner(make_tenant, make_user, email="owner@example.com"):
    tenant = make_tenant(name="Home")
    owner = make_user(email)
    Membership.objects.create(user=owner, tenant=tenant, role=Role.OWNER)
    return tenant, owner


def _c(tenant, path=""):
    return f"/t/{tenant.schema_name}/contacts/{path}"


def test_person_form_persists_household_flag(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    resp = client.post(
        _c(tenant, "people/new/"),
        {"first_name": "Asha", "last_name": "Kumar", "gender": "F", "is_household_member": "true"},
    )
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        assert Person.objects.get(first_name="Asha").is_household_member is True


def test_people_list_household_filter_and_count(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        Person.objects.create(first_name="Asha", last_name="Kumar", is_household_member=True)
        Person.objects.create(first_name="Ravi", last_name="Kumar", is_household_member=True)
        Person.objects.create(first_name="Zubin", last_name="Mistry")  # external contact
        assert count_household_members() == 2

    client.force_login(owner)
    filtered = client.get(_c(tenant, "people/") + "?household=1").content.decode()
    assert "Asha" in filtered and "Ravi" in filtered
    assert "Zubin" not in filtered

    unfiltered = client.get(_c(tenant, "people/")).content.decode()
    assert "Zubin" in unfiltered  # external contact still visible without the filter
    assert "Household" in unfiltered  # the filter chip renders
