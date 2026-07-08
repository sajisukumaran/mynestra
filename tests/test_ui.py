"""P2 UI-kit smoke tests: styleguide gating, shell/launcher rendering, cotton components render."""

import pytest
from django.test import override_settings

from apps.tenants.models import Membership, Role


def test_styleguide_renders_under_debug(client):
    with override_settings(DEBUG=True):
        response = client.get("/styleguide/")
    assert response.status_code == 200
    body = response.content.decode()
    # A spread of components rendered their semantic classes (cotton wrapped them).
    for token in ("class=\"btn", "class=\"badge", "class=\"chip", "class=\"stat",
                  "class=\"app-tile", "class=\"app", "sidebar", "topbar", "swatches"):
        assert token in body, f"missing {token!r} in styleguide"


def test_styleguide_404_when_not_debug(client):
    with override_settings(DEBUG=False):
        response = client.get("/styleguide/")
    assert response.status_code == 404


def test_launcher_composes_app_tiles(make_tenant, make_user, client):
    tenant = make_tenant(name="Sharma Household")
    owner = make_user("owner@example.com")
    Membership.objects.create(user=owner, tenant=tenant, role=Role.OWNER)

    client.force_login(owner)
    response = client.get(f"/t/{tenant.schema_name}/")
    assert response.status_code == 200
    body = response.content.decode()
    assert "launcher-wrap" in body       # launcher chrome
    assert "app-tile" in body            # tiles composed via <c-app-tile>
    assert "Contacts" in body
    assert "topbar" in body              # shell topbar present
    assert "Sharma Household" in body    # tenant name in topbar/greeting


@pytest.mark.parametrize("template_name", ["base.html", "cotton/button.html"])
def test_key_templates_load(template_name):
    from django.template.loader import get_template

    assert get_template(template_name) is not None
