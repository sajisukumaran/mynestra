"""Standard/Expert accounting mode: default, the one-way-until-locked toggle, and the Setup
Mode screen in each state. Finance gating + resolver + COA-editor behavior are covered in
test_posting_resolver.py / test_coa_editor.py."""

import pytest
from django.db import connection

from apps.tenants.models import Membership, Role, Tenant


def _owner(make_tenant, make_user, name="Acme", email="owner@example.com"):
    tenant = make_tenant(name=name)
    owner = make_user(email)
    Membership.objects.create(user=owner, tenant=tenant, role=Role.OWNER)
    return tenant, owner


def _u(tenant, path=""):
    return f"/t/{tenant.schema_name}/setup/{path}"


def _mode(tenant):
    """Read the tenant's mode fresh from the public schema (client requests pin the connection)."""
    connection.set_schema_to_public()
    tenant.refresh_from_db()
    return tenant.accounting_mode


def _set(tenant, **fields):
    connection.set_schema_to_public()
    Tenant.objects.filter(pk=tenant.pk).update(**fields)


# --- Default + context ----------------------------------------------------------------------

def test_new_tenant_defaults_to_standard(make_tenant):
    tenant = make_tenant(name="Fresh")
    assert tenant.accounting_mode == Tenant.AccountingMode.STANDARD
    assert tenant.accounting_locked is False


def test_mode_page_reachable_by_owner(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    assert client.get(_u(tenant, "mode/")).status_code == 200


def test_mode_page_forbidden_to_member(make_tenant, make_user, client):
    tenant, _o = _owner(make_tenant, make_user)
    member = make_user("member@example.com")
    Membership.objects.create(user=member, tenant=tenant, role=Role.MEMBER)
    client.force_login(member)
    assert client.get(_u(tenant, "mode/")).status_code == 403


# --- The toggle -----------------------------------------------------------------------------

def test_owner_can_switch_to_expert(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    resp = client.post(_u(tenant, "mode/"), {"mode": "expert"})
    assert resp.status_code == 302
    assert _mode(tenant) == Tenant.AccountingMode.EXPERT


def test_can_switch_back_to_standard_while_unlocked(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    _set(tenant, accounting_mode=Tenant.AccountingMode.EXPERT)
    client.force_login(owner)
    client.post(_u(tenant, "mode/"), {"mode": "standard"})
    assert _mode(tenant) == Tenant.AccountingMode.STANDARD


def test_cannot_switch_back_to_standard_when_locked(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    _set(tenant, accounting_mode=Tenant.AccountingMode.EXPERT, accounting_locked=True)
    client.force_login(owner)
    client.post(_u(tenant, "mode/"), {"mode": "standard"})
    assert _mode(tenant) == Tenant.AccountingMode.EXPERT


# --- The Mode screen renders in each state --------------------------------------------------

@pytest.mark.parametrize(
    "mode,locked",
    [("standard", False), ("expert", False), ("expert", True)],
)
def test_mode_screen_renders_in_each_state(make_tenant, make_user, client, mode, locked):
    tenant, owner = _owner(make_tenant, make_user)
    _set(tenant, accounting_mode=mode, accounting_locked=locked)
    client.force_login(owner)
    assert client.get(_u(tenant, "mode/")).status_code == 200
