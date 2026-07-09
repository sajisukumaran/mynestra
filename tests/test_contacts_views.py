"""Contacts views: People list (search/filter/sort/paginate), create/edit, detail (Overview +
History), soft-delete/restore/hard-delete, address/date slide-over CRUD, and tenant isolation."""

from django.test import override_settings
from django_tenants.utils import schema_context

from apps.contacts.models import Address, ImportantDate, Person
from apps.setup.models import Category
from apps.tenants.models import Membership, Role


def _owner(make_tenant, make_user, name="Acme", email="owner@example.com"):
    tenant = make_tenant(name=name)
    owner = make_user(email)
    Membership.objects.create(user=owner, tenant=tenant, role=Role.OWNER)
    return tenant, owner


def _c(tenant, path=""):
    return f"/t/{tenant.schema_name}/contacts/{path}"


def test_contacts_root_renders_dashboard(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    resp = client.get(_c(tenant))
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Everyone your household keeps in touch with" in body  # dashboard page header


def test_people_list_search_filter_sort_paginate(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        doc = Category.objects.get(kind="PERSON", name="Doctor")
        for i in range(12):
            Person.objects.create(first_name=f"Person{i:02d}", last_name="Test")
        sanjay = Person.objects.create(first_name="Sanjay", last_name="Rao")
        sanjay.categories.add(doc)

    client.force_login(owner)
    # renders + paginates (13 people, 10/page; names sort ascending so Person00 is on page 1)
    body = client.get(_c(tenant, "people/")).content.decode()
    assert "Person00" in body
    assert "pager" in body
    # search
    b = client.get(_c(tenant, "people/?q=sanjay")).content.decode()
    assert "Sanjay Rao" in b and "Person00" not in b
    # category filter
    b = client.get(_c(tenant, f"people/?category={doc.id}")).content.decode()
    assert "Sanjay Rao" in b and "Person00" not in b
    # sort + page 2 resolve without error
    assert client.get(_c(tenant, "people/?sort=-added&page=2")).status_code == 200


def test_person_create_with_channels_and_category(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    with schema_context(tenant.schema_name):
        fam = Category.objects.filter(kind="PERSON").first()

    resp = client.post(_c(tenant, "people/new/"), {
        "first_name": "Rajesh", "last_name": "Sharma", "preferred_name": "Raj", "gender": "M",
        "dob_month": "3", "dob_year": "1974", "dob_day": "",
        "marital_status": "married", "is_deceased": "false", "languages": "Hindi, English",
        "channel_type": ["phone", "email"], "channel_value": ["+91 1", "raj@example.com"],
        "channel_label": ["Mobile", "Personal"], "channel_primary": ["1", "0"],
        "categories": [str(fam.id)], "notes": "Hi.",
    })
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        p = Person.objects.get(first_name="Rajesh", last_name="Sharma")
        assert p.dob.display == "XX-Mar-1974"
        assert p.channels.count() == 2
        assert p.primary_channel.value == "+91 1"
        assert list(p.categories.values_list("id", flat=True)) == [fam.id]
        assert p.languages == ["Hindi", "English"]


def test_person_create_invalid_partial_date_rerenders(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    resp = client.post(_c(tenant, "people/new/"), {
        "first_name": "Bad", "last_name": "Date", "gender": "U",
        "dob_day": "15", "dob_month": "", "dob_year": "",  # day without month
    })
    assert resp.status_code == 200
    assert "Choose a month" in resp.content.decode()
    with schema_context(tenant.schema_name):
        assert not Person.objects.filter(first_name="Bad").exists()


def test_person_edit(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    with schema_context(tenant.schema_name):
        p = Person.objects.create(first_name="Ann", last_name="Lee")
    resp = client.post(_c(tenant, f"people/{p.pk}/edit/"), {
        "first_name": "Ann", "last_name": "Lee", "gender": "F", "occupation": "Engineer",
    })
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        p.refresh_from_db()
        assert p.occupation == "Engineer"


def test_person_detail_overview_and_history(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    with schema_context(tenant.schema_name):
        p = Person.objects.create(first_name="Rajesh", last_name="Sharma", occupation="Architect")
    body = client.get(_c(tenant, f"people/{p.pk}/")).content.decode()
    assert "Rajesh Sharma" in body
    assert "Details" in body and "Architect" in body
    assert "Overview" in body and "History" in body
    assert "Created this contact" in body  # simple-history timeline


def test_address_and_importantdate_crud(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    with schema_context(tenant.schema_name):
        p = Person.objects.create(first_name="Rajesh", last_name="Sharma")

    client.post(_c(tenant, f"people/{p.pk}/addresses/new/"),
                {"label": "Home", "city": "Bengaluru", "is_primary": "on"})
    client.post(_c(tenant, f"people/{p.pk}/dates/new/"),
                {"label": "Retirement", "date_month": "3", "date_year": "2039", "date_day": ""})
    with schema_context(tenant.schema_name):
        assert p.addresses.count() == 1
        assert p.important_dates.get().date.display == "XX-Mar-2039"
        addr, date = p.addresses.get(), p.important_dates.get()

    # invalid date re-renders with error, reopened
    resp = client.post(_c(tenant, f"people/{p.pk}/dates/new/"),
                       {"label": "Bad", "date_day": "31", "date_month": "2", "date_year": "2001"})
    assert "not valid" in resp.content.decode()

    client.post(_c(tenant, f"people/{p.pk}/addresses/{addr.pk}/delete/"))
    client.post(_c(tenant, f"people/{p.pk}/dates/{date.pk}/delete/"))
    with schema_context(tenant.schema_name):
        assert p.addresses.count() == 0 and p.important_dates.count() == 0


def test_soft_delete_restore_and_hard_delete(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    with schema_context(tenant.schema_name):
        p = Person.objects.create(first_name="Gone", last_name="Soon")

    assert client.post(_c(tenant, f"people/{p.pk}/delete/")).status_code == 302
    with schema_context(tenant.schema_name):
        assert Person.objects.filter(pk=p.pk).count() == 0
        assert Person.all_objects.get(pk=p.pk).is_deleted

    body = client.get(f"/t/{tenant.schema_name}/setup/recently-deleted/").content.decode()
    assert "Gone Soon" in body

    assert client.post(
        f"/t/{tenant.schema_name}/setup/recently-deleted/people/{p.pk}/restore/"
    ).status_code == 302
    with schema_context(tenant.schema_name):
        assert Person.objects.filter(pk=p.pk).exists()


@override_settings(ALLOW_HARD_DELETE=True)
def test_hard_delete_when_allowed(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    with schema_context(tenant.schema_name):
        p = Person.objects.create(first_name="Zap", last_name="Me")
        p.delete()
    client.post(f"/t/{tenant.schema_name}/setup/recently-deleted/people/{p.pk}/delete/")
    with schema_context(tenant.schema_name):
        assert not Person.all_objects.filter(pk=p.pk).exists()


@override_settings(ALLOW_HARD_DELETE=False)
def test_hard_delete_blocked_when_disabled(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    with schema_context(tenant.schema_name):
        p = Person.objects.create(first_name="Keep", last_name="Me")
        p.delete()
    client.post(f"/t/{tenant.schema_name}/setup/recently-deleted/people/{p.pk}/delete/")
    with schema_context(tenant.schema_name):
        assert Person.all_objects.filter(pk=p.pk).exists()  # still there (hard-delete gated off)


def test_contacts_tenant_isolation(make_tenant, make_user, client):
    a, owner_a = _owner(make_tenant, make_user, name="Alpha", email="a@example.com")
    b, owner_b = _owner(make_tenant, make_user, name="Beta", email="b@example.com")
    with schema_context(a.schema_name):
        pa = Person.objects.create(first_name="Alpha", last_name="Only")
        Address.objects.create(person=pa, city="Here")
        ImportantDate.objects.create(person=pa, label="X", date_year=2000)

    # Owner of B never sees A's person, and A's pk doesn't resolve in B's schema.
    client.force_login(owner_b)
    assert "Alpha Only" not in client.get(_c(b, "people/")).content.decode()
    assert client.get(_c(b, f"people/{pa.pk}/")).status_code == 404
    # Owner of B cannot reach A at all (not a member).
    assert client.get(_c(a, "people/")).status_code == 403

    # Owner of A sees exactly their person.
    client.force_login(owner_a)
    assert client.get(_c(a, f"people/{pa.pk}/")).status_code == 200
    with schema_context(b.schema_name):
        assert Person.objects.count() == 0  # zero leak into B
