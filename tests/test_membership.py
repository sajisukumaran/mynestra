"""MembershipMiddleware: members reach their tenant; non-members are denied; anon is redirected."""

from apps.tenants.models import Membership, Role


def _url(tenant):
    return f"/t/{tenant.schema_name}/"


def test_member_reaches_tenant_nonmember_gets_403(make_tenant, make_user, client):
    tenant = make_tenant(name="Acme")
    member = make_user("member@example.com")
    outsider = make_user("outsider@example.com")
    Membership.objects.create(user=member, tenant=tenant, role=Role.MEMBER)

    client.force_login(member)
    assert client.get(_url(tenant)).status_code == 200

    client.force_login(outsider)
    assert client.get(_url(tenant)).status_code == 403


def test_anonymous_tenant_request_redirects_to_login(make_tenant, client):
    tenant = make_tenant(name="Acme")
    response = client.get(_url(tenant))
    assert response.status_code == 302
    assert "/login/" in response["Location"]


def test_cross_membership(make_tenant, make_user, client):
    a = make_tenant(name="A")
    b = make_tenant(name="B")
    u1 = make_user("u1@example.com")
    u2 = make_user("u2@example.com")
    Membership.objects.create(user=u1, tenant=a, role=Role.OWNER)
    Membership.objects.create(user=u2, tenant=b, role=Role.OWNER)
    Membership.objects.create(user=u1, tenant=b, role=Role.MEMBER)  # u1 belongs to both

    client.force_login(u1)
    assert client.get(_url(a)).status_code == 200
    assert client.get(_url(b)).status_code == 200

    client.force_login(u2)
    assert client.get(_url(b)).status_code == 200
    assert client.get(_url(a)).status_code == 403  # u2 not in A


def test_public_paths_are_unaffected(client):
    assert client.get("/health/").status_code == 200
