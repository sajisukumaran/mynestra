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
