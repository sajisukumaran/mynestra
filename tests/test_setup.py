"""P3 Setup app: owner gates, system-row locks, member management + last-owner guard,
appearance persistence, and tenant isolation."""

import pytest
from django.test import override_settings
from django_tenants.utils import schema_context

from apps.organizations.models import Branch, Organization
from apps.relationships.models import PersonOrgRelationshipType, RelationshipType
from apps.setup.models import Category
from apps.tenants.models import Invitation, Membership, Role


def _owner(make_tenant, make_user, name="Acme", email="owner@example.com"):
    tenant = make_tenant(name=name)
    owner = make_user(email)
    Membership.objects.create(user=owner, tenant=tenant, role=Role.OWNER)
    return tenant, owner


def _u(tenant, path=""):
    return f"/t/{tenant.schema_name}/setup/{path}"


SETUP_PAGES = [
    "", "categories/", "relationship-types/", "members/",
    "appearance/", "localization/", "profile/", "recently-deleted/",
]


# --- Owner gates ----------------------------------------------------------------------------

@pytest.mark.parametrize("page", SETUP_PAGES)
def test_owner_reaches_setup_pages(make_tenant, make_user, client, page):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    assert client.get(_u(tenant, page)).status_code == 200


@pytest.mark.parametrize("page", SETUP_PAGES)
def test_member_is_forbidden_from_setup(make_tenant, make_user, client, page):
    tenant, _owner_u = _owner(make_tenant, make_user)
    member = make_user("member@example.com")
    Membership.objects.create(user=member, tenant=tenant, role=Role.MEMBER)
    client.force_login(member)
    assert client.get(_u(tenant, page)).status_code == 403


def test_anonymous_setup_redirects_to_login(make_tenant, client):
    tenant = make_tenant(name="Acme")
    resp = client.get(_u(tenant))
    assert resp.status_code == 302
    assert "/login/" in resp["Location"]


# --- System-row locks -----------------------------------------------------------------------

def test_system_category_cannot_be_edited_or_deleted(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    with schema_context(tenant.schema_name):
        sys_cat = Category.objects.filter(is_system=True).first()
    # Direct crafted POSTs are refused server-side.
    assert client.post(_u(tenant, f"categories/{sys_cat.pk}/edit/"),
                       {"name": "HACKED", "color": "rose"}).status_code == 403
    assert client.post(_u(tenant, f"categories/{sys_cat.pk}/delete/")).status_code == 403
    with schema_context(tenant.schema_name):
        sys_cat.refresh_from_db()
        assert sys_cat.name != "HACKED"
        assert Category.objects.filter(pk=sys_cat.pk).exists()


def test_custom_category_lifecycle(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    assert client.post(_u(tenant, "categories/new/PERSON/"),
                       {"name": "Piano Teacher", "color": "violet"}).status_code == 302
    with schema_context(tenant.schema_name):
        cat = Category.objects.get(kind="PERSON", name="Piano Teacher")
        assert cat.is_system is False
    assert client.post(_u(tenant, f"categories/{cat.pk}/edit/"),
                       {"name": "Violin Teacher", "color": "amber"}).status_code == 302
    assert client.post(_u(tenant, f"categories/{cat.pk}/delete/")).status_code == 302
    with schema_context(tenant.schema_name):
        assert not Category.objects.filter(pk=cat.pk).exists()


def test_system_relationship_types_are_locked(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    with schema_context(tenant.schema_name):
        sys_p2p = RelationshipType.objects.filter(is_system=True).first()
        sys_p2o = PersonOrgRelationshipType.objects.filter(is_system=True).first()
    p2p_delete = client.post(_u(tenant, f"relationship-types/p2p/{sys_p2p.pk}/delete/"))
    p2o_delete = client.post(_u(tenant, f"relationship-types/p2o/{sys_p2o.pk}/delete/"))
    assert p2p_delete.status_code == 403
    assert p2o_delete.status_code == 403
    with schema_context(tenant.schema_name):
        assert RelationshipType.objects.filter(pk=sys_p2p.pk).exists()


def test_custom_p2p_type_create(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    resp = client.post(_u(tenant, "relationship-types/p2p/new/"), {
        "code": "godparent_godchild", "is_symmetric": "false",
        "a_label_m": "Godfather", "a_label_f": "Godmother", "a_label_n": "Godparent",
        "b_label_m": "Godson", "b_label_f": "Goddaughter", "b_label_n": "Godchild",
    })
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        t = RelationshipType.objects.get(code="godparent_godchild")
        assert t.is_system is False
        assert t.b_label_f == "Goddaughter"


# --- Members & invitations ------------------------------------------------------------------

def test_invite_revoke_resend(make_tenant, make_user, client, mailoutbox):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    assert client.post(_u(tenant, "members/invite/"),
                       {"email": "new@example.com", "role": "MEMBER"}).status_code == 302
    inv = Invitation.objects.get(tenant=tenant, email="new@example.com")
    assert len(mailoutbox) == 1

    assert client.post(_u(tenant, f"invitations/{inv.pk}/resend/")).status_code == 302
    assert len(mailoutbox) == 2  # resent

    assert client.post(_u(tenant, f"invitations/{inv.pk}/revoke/")).status_code == 302
    inv.refresh_from_db()
    assert inv.status == Invitation.Status.REVOKED


def test_role_change_and_remove(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    member = make_user("member@example.com")
    m = Membership.objects.create(user=member, tenant=tenant, role=Role.MEMBER)
    client.force_login(owner)

    assert client.post(_u(tenant, f"members/{m.pk}/role/"), {"role": "OWNER"}).status_code == 302
    m.refresh_from_db()
    assert m.role == Role.OWNER

    assert client.post(_u(tenant, f"members/{m.pk}/remove/")).status_code == 302
    assert not Membership.objects.filter(pk=m.pk).exists()


def test_last_owner_guard(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    om = Membership.objects.get(user=owner, tenant=tenant)
    client.force_login(owner)
    # Sole owner cannot be demoted or removed.
    assert client.post(_u(tenant, f"members/{om.pk}/role/"), {"role": "MEMBER"}).status_code == 403
    assert client.post(_u(tenant, f"members/{om.pk}/remove/")).status_code == 403
    om.refresh_from_db()
    assert om.role == Role.OWNER


# --- Appearance -----------------------------------------------------------------------------

def test_palette_persists_and_recolors(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    assert client.post(_u(tenant, "appearance/"),
                       {"palette": "indigo", "theme": "dark"}).status_code == 302
    tenant.refresh_from_db()
    owner.refresh_from_db()
    assert tenant.palette == "indigo"
    assert owner.theme == "dark"
    # Server renders the household palette + personal theme onto <html>.
    body = client.get(_u(tenant)).content.decode()
    assert 'var sp = "indigo"' in body
    assert 'var st = "dark"' in body


def test_theme_endpoint_persists_per_user(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    assert client.post("/theme/", {"theme": "dark"}).status_code == 204
    owner.refresh_from_db()
    assert owner.theme == "dark"
    assert client.post("/theme/", {"theme": ""}).status_code == 204
    owner.refresh_from_db()
    assert owner.theme is None


def test_invalid_palette_ignored(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    client.post(_u(tenant, "appearance/"), {"palette": "chartreuse", "theme": ""})
    tenant.refresh_from_db()
    assert tenant.palette == "teal"  # unchanged (invalid value rejected)


# --- Localization ---------------------------------------------------------------------------

def test_localization_persists(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    resp = client.post(
        _u(tenant, "localization/"),
        {"currency": "EUR", "timezone": "Europe/London", "date_format": "dmy",
         "number_format": "indian"},
    )
    assert resp.status_code == 302
    tenant.refresh_from_db()
    assert tenant.currency == "EUR"
    assert tenant.timezone == "Europe/London"
    assert tenant.date_format == "dmy"
    assert tenant.number_format == "indian"


def test_invalid_currency_ignored(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    client.post(_u(tenant, "localization/"), {"currency": "ZZZ", "timezone": "Mars/Base"})
    tenant.refresh_from_db()
    assert tenant.currency == "USD"  # unchanged (not in the Currency catalog)
    assert tenant.timezone == "UTC"  # unchanged (not in CURATED_TIMEZONES)


def test_ui_currency_exposed_to_templates(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    client.post(_u(tenant, "localization/"), {"currency": "GBP"})
    resp = client.get(_u(tenant, "localization/"))
    assert resp.status_code == 200
    assert resp.context["ui_currency"] == "GBP"


# --- Tenant isolation (hard gate) -----------------------------------------------------------

def test_owner_of_a_cannot_touch_tenant_b(make_tenant, make_user, client):
    a, owner_a = _owner(make_tenant, make_user, name="Alpha", email="a@example.com")
    b, owner_b = _owner(make_tenant, make_user, name="Beta", email="b@example.com")

    client.force_login(owner_a)
    # Owner of A is not a member of B: every Setup route under B is 403.
    for page in SETUP_PAGES:
        assert client.get(_u(b, page)).status_code == 403

    # An invitation created in B cannot be revoked through A's URL (404 — not found in A).
    inv_b = Invitation.objects.create(email="x@example.com", tenant=b, role=Role.MEMBER)
    assert client.post(_u(a, f"invitations/{inv_b.pk}/revoke/")).status_code == 404
    inv_b.refresh_from_db()
    assert inv_b.status == Invitation.Status.PENDING

    # A membership in B cannot be removed through A's URL.
    mb = Membership.objects.get(user=owner_b, tenant=b)
    assert client.post(_u(a, f"members/{mb.pk}/remove/")).status_code == 404
    assert Membership.objects.filter(pk=mb.pk).exists()


def test_palette_change_isolated_to_tenant(make_tenant, make_user, client):
    a, owner_a = _owner(make_tenant, make_user, name="Alpha", email="a@example.com")
    b, _owner_b = _owner(make_tenant, make_user, name="Beta", email="b@example.com")

    client.force_login(owner_a)
    client.post(_u(a, "appearance/"), {"palette": "violet", "theme": ""})
    a.refresh_from_db()
    b.refresh_from_db()
    assert a.palette == "violet"
    assert b.palette == "teal"  # B unaffected


# --- Seed reachability ----------------------------------------------------------------------

def test_new_tenant_seeds_are_present_and_locked(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    body = client.get(_u(tenant, "categories/")).content.decode()
    assert "Bank" in body and "Doctor" in body   # seeded org + person categories
    assert "badge-lock" in body                   # system rows show a lock
    with schema_context(tenant.schema_name):
        assert Category.objects.filter(is_system=True).count() >= 18
        assert RelationshipType.objects.filter(is_system=True).exists()
        assert PersonOrgRelationshipType.objects.filter(is_system=True).exists()


# --- Recently deleted: branches (restore + hard-delete gating) -------------------------------

def _deleted_branch(tenant):
    with schema_context(tenant.schema_name):
        org = Organization.objects.create(name="HDFC Bank")
        branch = Branch.objects.create(organization=org, name="MG Road")
        branch.delete()  # soft
        return branch.pk


def test_recently_deleted_lists_and_restores_branch(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    bid = _deleted_branch(tenant)
    client.force_login(owner)
    rd = _u(tenant, "recently-deleted/")

    assert "MG Road" in client.get(rd).content.decode()
    assert client.post(rd + f"branches/{bid}/restore/").status_code == 302
    with schema_context(tenant.schema_name):
        assert Branch.objects.filter(pk=bid).exists()  # back among live rows


@override_settings(ALLOW_HARD_DELETE=False)
def test_branch_hard_delete_blocked_when_disallowed(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    bid = _deleted_branch(tenant)
    client.force_login(owner)
    client.post(_u(tenant, f"recently-deleted/branches/{bid}/delete/"))
    with schema_context(tenant.schema_name):
        assert Branch.all_objects.filter(pk=bid).exists()  # still soft-deleted, not purged


@override_settings(ALLOW_HARD_DELETE=True)
def test_branch_hard_delete_when_allowed(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    bid = _deleted_branch(tenant)
    client.force_login(owner)
    client.post(_u(tenant, f"recently-deleted/branches/{bid}/delete/"))
    with schema_context(tenant.schema_name):
        assert not Branch.all_objects.filter(pk=bid).exists()  # purged for good
