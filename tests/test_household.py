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


def _s(tenant, path=""):
    return f"/t/{tenant.schema_name}/setup/{path}"


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


# --- Setup → Household members screen (owner-only) -------------------------------------------

def test_setup_household_add_and_remove(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        person = Person.objects.create(first_name="Meera", last_name="Kumar")
    client.force_login(owner)

    assert client.get(_s(tenant, "household-members/")).status_code == 200

    added = client.post(_s(tenant, "household-members/add/"), {"person": person.pk})
    assert added.status_code == 302
    with schema_context(tenant.schema_name):
        person.refresh_from_db()
        assert person.is_household_member is True

    resp = client.post(_s(tenant, f"household-members/{person.pk}/remove/"))
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        person.refresh_from_db()
        assert person.is_household_member is False


def test_setup_household_search_excludes_members(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        Person.objects.create(first_name="Asha", last_name="Kumar", is_household_member=True)
        Person.objects.create(first_name="Zubin", last_name="Mistry")  # candidate
    client.force_login(owner)
    body = client.get(_s(tenant, "household-members/search/") + "?q=Kumar").content.decode()
    # Asha is already a member → not a candidate; Zubin doesn't match "Kumar" → also absent.
    assert "Asha" not in body
    body2 = client.get(_s(tenant, "household-members/search/") + "?q=Zubin").content.decode()
    assert "Zubin" in body2


def test_setup_household_is_owner_only(make_tenant, make_user, client):
    tenant, _owner_user = _owner(make_tenant, make_user)
    member = make_user("member@example.com")
    Membership.objects.create(user=member, tenant=tenant, role=Role.MEMBER)
    client.force_login(member)
    assert client.get(_s(tenant, "household-members/")).status_code == 403
