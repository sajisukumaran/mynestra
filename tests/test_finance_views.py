"""Finance UI: read-only Chart-of-Accounts page + the live launcher tile."""

import datetime
from decimal import Decimal

from django.apps import apps as django_apps
from django.db import connection
from django_tenants.utils import schema_context

from apps.finance.services import LineInput, post_entry
from apps.tenants.models import Membership, Role, Tenant

D = Decimal


def _member(make_tenant, make_user, role=Role.MEMBER):
    tenant = make_tenant(name="Ledgerton")
    user = make_user("u@example.com")
    Membership.objects.create(user=user, tenant=tenant, role=role)
    return tenant, user


def _expert(tenant):
    """Finance is Expert-mode only; reveal it for these tests."""
    connection.set_schema_to_public()
    Tenant.objects.filter(pk=tenant.pk).update(accounting_mode="expert")


def _url(tenant, path=""):
    return f"/t/{tenant.schema_name}/finance/{path}"


def test_member_reaches_chart_of_accounts(make_tenant, make_user, client):
    tenant, user = _member(make_tenant, make_user)
    _expert(tenant)
    client.force_login(user)
    resp = client.get(_url(tenant))
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Chart of accounts" in body
    assert "Opening Balance Equity" in body  # seeded equity account rendered
    assert "Assets" in body and "Liabilities" in body and "Equity" in body


def test_finance_hidden_in_standard(make_tenant, make_user, client):
    tenant, user = _member(make_tenant, make_user)  # default Standard
    client.force_login(user)
    assert client.get(_url(tenant)).status_code == 404


def test_non_member_is_denied(make_tenant, make_user, client):
    tenant, _u = _member(make_tenant, make_user)
    _expert(tenant)
    outsider = make_user("outsider@example.com")  # no membership in this tenant
    client.force_login(outsider)
    assert client.get(_url(tenant)).status_code == 403


def test_posted_balance_shows_on_page(make_tenant, make_user, client):
    tenant, user = _member(make_tenant, make_user)
    _expert(tenant)
    with schema_context(tenant.schema_name):
        post_entry(
            date=datetime.date(2026, 1, 10),
            lines=[
                LineInput("1110", debit=D("1500")),
                LineInput("opening_balance_equity", credit=D("1500")),
            ],
        )
    client.force_login(user)
    body = client.get(_url(tenant)).content.decode()
    assert "1,500.00" in body  # cash balance rendered via c-money
    assert 'class="amount' in body  # the money component is used


def test_launcher_tile_shows_finance_in_expert(make_tenant, make_user, client):
    tenant, user = _member(make_tenant, make_user)
    _expert(tenant)
    client.force_login(user)
    body = client.get(f"/t/{tenant.schema_name}/").content.decode()
    assert "Finance" in body
    assert "finance/" in body  # tile links to the finance app


def test_launcher_hides_finance_in_standard(make_tenant, make_user, client):
    tenant, user = _member(make_tenant, make_user)  # default Standard
    client.force_login(user)
    body = client.get(f"/t/{tenant.schema_name}/").content.decode()
    assert "finance/" not in body  # Finance tile hidden in Standard mode


def test_launcher_counts_are_live(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        config = django_apps.get_app_config("finance")
        counts = {c["label"]: c["n"] for c in config.launcher_counts()}
    # Seeded catalogs: postable accounts present, 16 currencies, no journal entries yet.
    assert counts["Currencies"] == 16
    assert counts["Journal entries"] == 0
    assert counts["Accounts"] > 0
