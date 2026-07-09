"""On-brand error pages (DESIGN §8): 404 for unknown paths, 403 for non-members (via
PermissionDenied in MembershipMiddleware), and the standalone 500 template. Test settings run
with DEBUG=False, so Django's handlers render our templates/{403,404,500}.html, not debug pages."""

from django.template.loader import render_to_string

from apps.tenants.models import Membership, Role


def test_unknown_path_renders_on_brand_404(make_tenant, make_user, client):
    tenant = make_tenant(name="Acme")
    owner = make_user("owner@example.com")
    Membership.objects.create(user=owner, tenant=tenant, role=Role.OWNER)
    client.force_login(owner)

    resp = client.get(f"/t/{tenant.schema_name}/contacts/does-not-exist/")
    assert resp.status_code == 404
    body = resp.content.decode()
    assert "Page not found" in body and "404" in body


def test_non_member_gets_on_brand_403(make_tenant, make_user, client):
    tenant = make_tenant(name="Acme")
    outsider = make_user("outsider@example.com")  # no membership
    client.force_login(outsider)

    resp = client.get(f"/t/{tenant.schema_name}/")
    assert resp.status_code == 403
    body = resp.content.decode()
    assert "Access denied" in body and "403" in body


def test_500_template_renders_standalone():
    # handler500 renders with an empty context (no request / no context processors); assert the
    # template does not depend on any of that.
    html = render_to_string("500.html")
    assert "Something went wrong" in html and "500" in html
