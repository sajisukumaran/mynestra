"""Organizations: CRUD lifecycle (inline channels + identifiers), the ORG category filter incl.
the locked "Bank" seam future modules rely on, and soft-delete/restore."""

from django.test import override_settings
from django_tenants.utils import schema_context

from apps.organizations.models import Organization
from apps.setup.models import Category
from apps.tenants.models import Membership, Role


def _owner(make_tenant, make_user, name="Acme", email="owner@example.com"):
    tenant = make_tenant(name=name)
    owner = make_user(email)
    Membership.objects.create(user=owner, tenant=tenant, role=Role.OWNER)
    return tenant, owner


def _o(tenant, path=""):
    return f"/t/{tenant.schema_name}/organizations/{path}"


def test_org_create_with_channels_and_identifiers(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    resp = client.post(_o(tenant, "new/"), {
        "name": "HDFC Bank", "display_name": "HDFC", "website": "https://hdfc.example",
        "channel_type": ["phone", "url"], "channel_value": ["+91 22 100", "hdfc.example"],
        "channel_label": ["Head office", "Site"], "channel_primary": ["1", "0"],
        "identifier_type": ["GST", ""], "identifier_value": ["29ABCDE", "skipme"],
        "notes": "Primary bank.",
    })
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        o = Organization.objects.get(name="HDFC Bank")
        assert o.display == "HDFC"
        assert o.channels.count() == 2
        assert o.primary_channel.value == "+91 22 100"
        assert o.identifiers.count() == 1  # the row missing a type is skipped
        assert o.identifiers.get().value == "29ABCDE"


def test_org_list_filter_by_bank_category(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        bank = Category.objects.get(kind="ORG", name="Bank")
        hdfc = Organization.objects.create(name="HDFC Bank")
        hdfc.categories.add(bank)
        Organization.objects.create(name="Green School")
        bank_id = bank.id

    client.force_login(owner)
    body = client.get(_o(tenant, f"all/?category={bank_id}")).content.decode()
    assert "HDFC Bank" in body and "Green School" not in body
    # Unfiltered shows both.
    allbody = client.get(_o(tenant, "all/")).content.decode()
    assert "HDFC Bank" in allbody and "Green School" in allbody


def test_org_detail_and_edit(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    with schema_context(tenant.schema_name):
        o = Organization.objects.create(name="HDFC Bank", website="https://hdfc.example")
    body = client.get(_o(tenant, f"{o.pk}/")).content.decode()
    for marker in ("HDFC Bank", "Overview", "Branches", "Key people"):
        assert marker in body

    client.post(_o(tenant, f"{o.pk}/edit/"), {"name": "HDFC Bank", "display_name": "HDFC Ltd"})
    with schema_context(tenant.schema_name):
        o.refresh_from_db()
        assert o.display_name == "HDFC Ltd"


def test_org_soft_delete_restore(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    with schema_context(tenant.schema_name):
        o = Organization.objects.create(name="Gone Corp")
        oid = o.pk
    assert client.post(_o(tenant, f"{oid}/delete/")).status_code == 302
    with schema_context(tenant.schema_name):
        assert Organization.objects.filter(pk=oid).count() == 0
        assert Organization.all_objects.get(pk=oid).is_deleted

    rd = f"/t/{tenant.schema_name}/setup/recently-deleted/"
    assert "Gone Corp" in client.get(rd).content.decode()
    assert client.post(rd + f"organizations/{oid}/restore/").status_code == 302
    with schema_context(tenant.schema_name):
        assert Organization.objects.filter(pk=oid).exists()


@override_settings(ALLOW_HARD_DELETE=True)
def test_org_hard_delete_when_allowed(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    with schema_context(tenant.schema_name):
        o = Organization.objects.create(name="Zap Corp")
        o.delete()
        oid = o.pk
    client.post(f"/t/{tenant.schema_name}/setup/recently-deleted/organizations/{oid}/delete/")
    with schema_context(tenant.schema_name):
        assert not Organization.all_objects.filter(pk=oid).exists()


def test_detail_sub_edits_use_popups_not_drawers(make_tenant, make_user, client):
    """Detail sub-edits (address / branch / etc.) are centered popups (overlay center), not side
    drawers (overlay right) — applied app-wide across Organization, Person and Family details."""
    from apps.contacts.models import Person
    from apps.families.models import Family

    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        oid = Organization.objects.create(name="HDFC Bank").pk
        pid = Person.objects.create(first_name="Rajesh", last_name="Sharma").pk
        fid = Family.objects.create(name="Sharma").pk

    client.force_login(owner)
    base = f"/t/{tenant.schema_name}/"
    for path in (f"organizations/{oid}/", f"contacts/people/{pid}/", f"contacts/families/{fid}/"):
        body = client.get(base + path).content.decode()
        assert "overlay right" not in body, path   # no side-drawers left
        assert "overlay center" in body, path       # modals present
