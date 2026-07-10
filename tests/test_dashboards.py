"""P7 dashboards + launcher live counts (DESIGN §8/§9), and cross-tenant count isolation.

Screen tests drive the authenticated tenant client; the upcoming-dates feed is also exercised
directly (service level) with a fixed reference date so the birthday/anniversary matrix is
deterministic regardless of the day the suite runs.
"""

import datetime

from django.utils import timezone
from django_tenants.utils import schema_context

from apps.contacts.models import ImportantDate, Person
from apps.families.models import Family
from apps.organizations.models import Branch, Organization
from apps.setup.models import Category
from apps.tenants.models import Membership, Role


def _owner(make_tenant, make_user, name="Acme", email="owner@example.com"):
    tenant = make_tenant(name=name)
    owner = make_user(email)
    Membership.objects.create(user=owner, tenant=tenant, role=Role.OWNER)
    return tenant, owner


# --- Upcoming-dates feed (service level, deterministic reference date) ----------------------

def test_upcoming_dates_feed(make_tenant):
    from apps.contacts.services import upcoming_dates

    tenant = make_tenant()
    on = datetime.date(2026, 3, 14)
    with schema_context(tenant.schema_name):
        aarav = Person.objects.create(
            first_name="Aarav", last_name="Sharma", dob_year=2009, dob_month=3, dob_day=14
        )
        dead = Person.objects.create(
            first_name="Gone", last_name="Away", is_deceased=True,
            dob_year=1950, dob_month=3, dob_day=14,
        )
        Person.objects.create(
            first_name="Priya", last_name="Sharma",
            anniversary_year=2007, anniversary_month=3, anniversary_day=20,
        )
        meera = Person.objects.create(first_name="Meera", last_name="Nair", dob_month=3)  # no year
        ImportantDate.objects.create(person=aarav, label="Retirement", date_month=3, date_day=25)

        rows = upcoming_dates(30, on=on)

        assert any(
            r.title == "Aarav Sharma" and r.kind == "birthday" and "Turns 17" in r.subtitle
            for r in rows
        )
        assert not any(r.person.pk == dead.pk and r.kind == "birthday" for r in rows)  # deceased
        assert any(r.kind == "anniversary" and "19th anniversary" in r.subtitle for r in rows)
        assert any(r.person.pk == meera.pk and not r.occ.day_known for r in rows)  # month-only
        assert any(r.kind == "date" and r.subtitle == "Retirement" for r in rows)
        # sorted soonest-first
        assert [r.occ.days_away for r in rows] == sorted(r.occ.days_away for r in rows)


# --- Contacts dashboard (HTTP) --------------------------------------------------------------

def test_contacts_dashboard_renders_counts_and_upcoming(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    today = timezone.localdate()
    with schema_context(tenant.schema_name):
        Person.objects.create(
            first_name="Aarav", last_name="Sharma",
            dob_year=2009, dob_month=today.month, dob_day=today.day,  # birthday today
        )
        Person.objects.create(first_name="Meera", last_name="Nair")
        Family.objects.create(name="Sharma")

    client.force_login(owner)
    body = client.get(f"/t/{tenant.schema_name}/contacts/").content.decode()
    assert "Everyone your household keeps in touch with" in body  # dashboard header
    assert "Aarav Sharma" in body                                 # upcoming birthday row
    assert "Birthdays · 30 days" in body                          # birthdays stat tile


# --- Important dates screen -----------------------------------------------------------------

def test_important_dates_screen(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    today = timezone.localdate()
    soon = today + datetime.timedelta(days=200)  # inside the year-ahead window, outside 30 days
    with schema_context(tenant.schema_name):
        Person.objects.create(
            first_name="Kamala", last_name="Sharma",
            dob_year=1955, dob_month=soon.month, dob_day=soon.day,
        )
    client.force_login(owner)
    resp = client.get(f"/t/{tenant.schema_name}/contacts/dates/")
    assert resp.status_code == 200
    assert "Kamala Sharma" in resp.content.decode()


def test_important_dates_empty_state(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    body = client.get(f"/t/{tenant.schema_name}/contacts/dates/").content.decode()
    assert "No dates yet" in body


# --- Organizations dashboard + URL restructure ----------------------------------------------

def test_org_dashboard_is_landing_with_counts_and_category_seam(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        bank = Category.objects.get(kind="ORG", name="Bank")
        hdfc = Organization.objects.create(name="HDFC Bank")
        hdfc.categories.add(bank)
        Branch.objects.create(organization=hdfc, name="MG Road", is_primary=True)
        bank_id = bank.id

    client.force_login(owner)
    body = client.get(f"/t/{tenant.schema_name}/organizations/").content.decode()
    assert "your household deals with" in body                     # dashboard header (landing)
    assert "HDFC Bank" in body                                     # recently added
    assert f"organizations/all/?category={bank_id}" in body        # by-category → filtered list


def test_org_list_moved_to_all(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    assert client.get(f"/t/{tenant.schema_name}/organizations/all/").status_code == 200


# --- Launcher live counts + module registry (DESIGN §9) -------------------------------------

def _launcher_counts(app_label):
    from django.apps import apps as django_apps

    return {c["label"]: c["n"] for c in django_apps.get_app_config(app_label).launcher_counts()}


def test_launcher_counts_reflect_live_data(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        Person.objects.create(first_name="A", last_name="One")
        Person.objects.create(first_name="B", last_name="Two")
        Family.objects.create(name="Fam")
        Organization.objects.create(name="HDFC Bank")

        contacts = _launcher_counts("contacts")
        assert contacts["People"] == 2 and contacts["Families"] == 1
        orgs = _launcher_counts("organizations")
        assert orgs["Organizations"] == 1 and orgs["Branches"] == 0


def test_launcher_renders_enabled_and_coming_soon_tiles(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    body = client.get(f"/t/{tenant.schema_name}/").content.decode()
    # Enabled tiles (+ their live counts) and a still-coming-soon tile (Health).
    for label in ("Contacts", "Organizations", "People", "Branches", "Key people", "Health"):
        assert label in body


def test_banking_launcher_tile_is_live(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        from apps.banking.models import BankAccount

        bank = Organization.objects.create(name="HDFC Bank")
        bank.categories.add(Category.objects.get(kind="ORG", name="Bank"))
        BankAccount.objects.create(
            bank=bank, account_type="checking", nickname="Chk", currency_id="USD"
        )
        counts = _launcher_counts("banking")
    assert counts["Accounts"] == 1 and counts["Banks"] == 1

    client.force_login(owner)
    body = client.get(f"/t/{tenant.schema_name}/").content.decode()
    assert "Banking" in body and "banking/" in body  # live tile links to the app


def test_launcher_counts_are_tenant_isolated(make_tenant):
    a = make_tenant(name="Alpha")
    b = make_tenant(name="Beta")
    with schema_context(a.schema_name):
        for i in range(3):
            Person.objects.create(first_name=f"A{i}", last_name="X")
    with schema_context(b.schema_name):
        Person.objects.create(first_name="Bonly", last_name="Y")

    with schema_context(a.schema_name):
        assert _launcher_counts("contacts")["People"] == 3
    with schema_context(b.schema_name):
        assert _launcher_counts("contacts")["People"] == 1  # no leak from Alpha
