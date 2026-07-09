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
