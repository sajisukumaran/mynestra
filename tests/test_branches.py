"""Branches: CRUD and their own (branch-owned) channels/addresses (DESIGN §5)."""

from django_tenants.utils import schema_context

from apps.contacts.models import Address, ContactChannel
from apps.organizations.models import Branch, Organization
from apps.tenants.models import Membership, Role


def _owner(make_tenant, make_user, name="Acme", email="owner@example.com"):
    tenant = make_tenant(name=name)
    owner = make_user(email)
    Membership.objects.create(user=owner, tenant=tenant, role=Role.OWNER)
    return tenant, owner


def test_branch_crud_with_own_channels_and_addresses(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    with schema_context(tenant.schema_name):
        org = Organization.objects.create(name="HDFC Bank")
        oid = org.pk
    base = f"/t/{tenant.schema_name}/organizations/{oid}/"

    client.post(base + "branches/new/", {"name": "Church Street", "is_primary": "on"})
    with schema_context(tenant.schema_name):
        branch = Branch.objects.get(name="Church Street")
        assert branch.is_primary
        bid = branch.pk

    # A branch owns its own channel + address (satisfies the 4-way owner CHECK).
    client.post(
        f"{base}branches/{bid}/channels/new/",
        {"type": "phone", "value": "+91 80 4000", "label": "Front desk", "is_primary": "on"},
    )
    client.post(
        f"{base}branches/{bid}/addresses/new/", {"city": "Bengaluru", "line1": "12 Church St"}
    )
    with schema_context(tenant.schema_name):
        branch = Branch.objects.get(pk=bid)
        assert branch.channels.count() == 1 and branch.addresses.count() == 1
        ch = branch.channels.get()
        assert ch.person_id is None and ch.organization_id is None and ch.branch_id == bid

    # Rendered on the org's Branches tab.
    body = client.get(base).content.decode()
    assert "Church Street" in body and "+91 80 4000" in body

    # Delete the branch (soft) — it hides from the org's Branches tab.
    client.post(f"{base}branches/{bid}/delete/")
    with schema_context(tenant.schema_name):
        assert Branch.objects.filter(pk=bid).count() == 0


def test_branch_channel_and_address_edit_delete(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    with schema_context(tenant.schema_name):
        org = Organization.objects.create(name="HDFC Bank")
        branch = Branch.objects.create(organization=org, name="Main")
        ch = ContactChannel.objects.create(branch=branch, type="phone", value="+91 1")
        addr = Address.objects.create(branch=branch, city="Old City")
        oid, bid, chid, aid = org.pk, branch.pk, ch.pk, addr.pk
    base = f"/t/{tenant.schema_name}/organizations/{oid}/branches/{bid}/"

    client.post(f"{base}channels/{chid}/edit/", {"type": "email", "value": "ops@hdfc.example"})
    client.post(f"{base}addresses/{aid}/edit/", {"city": "New City"})
    with schema_context(tenant.schema_name):
        ch.refresh_from_db()
        addr.refresh_from_db()
        assert ch.type == "email" and ch.value == "ops@hdfc.example"
        assert addr.city == "New City"

    client.post(f"{base}channels/{chid}/delete/")
    client.post(f"{base}addresses/{aid}/delete/")
    with schema_context(tenant.schema_name):
        assert ContactChannel.objects.filter(pk=chid).count() == 0
        assert Address.objects.filter(pk=aid).count() == 0


def test_branch_popup_number_dates_and_folded_address(make_tenant, make_user, client):
    """The branch popup carries a number, Opened/Closed PartialDates, and a folded primary
    address (created on add, updated in place on edit)."""
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    with schema_context(tenant.schema_name):
        oid = Organization.objects.create(name="HDFC Bank").pk
    base = f"/t/{tenant.schema_name}/organizations/{oid}/"

    client.post(base + "branches/new/", {
        "name": "MG Road", "number": "HDFC0001234", "is_primary": "on",
        "opened_year": "1998", "opened_month": "6", "opened_day": "",
        "line1": "12 MG Road", "city": "Bengaluru", "label": "Branch office",
    })
    with schema_context(tenant.schema_name):
        b = Branch.objects.get(name="MG Road")
        assert b.number == "HDFC0001234"
        assert (b.opened_year, b.opened_month, b.opened_day) == (1998, 6, None)
        assert not b.is_closed
        addr = b.addresses.get()
        assert addr.is_primary and addr.city == "Bengaluru" and addr.line1 == "12 MG Road"
        bid = b.pk

    # Edit: set a closed date and update the folded primary address in place (no duplicate row).
    client.post(f"{base}branches/{bid}/edit/", {
        "name": "MG Road", "number": "HDFC0001234", "is_primary": "on",
        "opened_year": "1998", "opened_month": "6", "opened_day": "",
        "closed_year": "2020", "closed_month": "3", "closed_day": "",
        "line1": "12 MG Road", "city": "Mysuru", "label": "Branch office",
    })
    with schema_context(tenant.schema_name):
        b = Branch.objects.get(pk=bid)
        assert b.is_closed and b.closed_year == 2020
        assert b.addresses.count() == 1
        assert b.addresses.get().city == "Mysuru"

    # The branch card surfaces the closed date + a Closed badge.
    body = client.get(base).content.decode()
    assert "XX-Mar-2020" in body        # closed date display on the branch card
    assert "badge-warning" in body      # the Closed badge (only warning badge on this page)


def test_branch_popup_folded_phone(make_tenant, make_user, client):
    """The branch popup carries a `phone` that upserts the branch's primary phone channel:
    created on add, updated in place on edit (no duplicate channel), and prefilled on the form."""
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    with schema_context(tenant.schema_name):
        oid = Organization.objects.create(name="HDFC Bank").pk
    base = f"/t/{tenant.schema_name}/organizations/{oid}/"

    client.post(base + "branches/new/", {"name": "MG Road", "phone": "+91 80 4000 1234"})
    with schema_context(tenant.schema_name):
        b = Branch.objects.get(name="MG Road")
        ch = b.channels.get()
        assert ch.type == "phone" and ch.value == "+91 80 4000 1234" and ch.is_primary
        bid, chid = b.pk, ch.pk

    # Prefilled on the edit popup.
    assert 'value="+91 80 4000 1234"' in client.get(base).content.decode()

    # Edit updates the same channel in place — no duplicate row.
    client.post(f"{base}branches/{bid}/edit/", {"name": "MG Road", "phone": "+91 80 9999 0000"})
    with schema_context(tenant.schema_name):
        b = Branch.objects.get(pk=bid)
        assert b.channels.count() == 1
        assert b.channels.get().pk == chid and b.channels.get().value == "+91 80 9999 0000"

    # Blank phone on edit is a no-op (keeps the existing channel).
    client.post(f"{base}branches/{bid}/edit/", {"name": "MG Road", "phone": ""})
    with schema_context(tenant.schema_name):
        assert Branch.objects.get(pk=bid).channels.count() == 1
