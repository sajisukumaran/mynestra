"""Institutions (brokerages): the grouped index + per-brokerage totals, adding a brokerage with
minimal info, the institution detail (accounts breakdown + branches), inline edit, and the
account-create institution prefill. Reuses the Brokerage-category seam — no schema change."""

import datetime
from decimal import Decimal

from django_tenants.utils import schema_context

from apps.finance.models import Currency
from apps.investments.models import InvestmentAccount, InvestmentTransaction, InvTxnType
from apps.investments.services import apply_transaction, ensure_gl_account, institution_summary
from apps.organizations.models import Branch, Organization
from apps.setup.models import Category
from apps.tenants.models import Membership, Role

D = Decimal
JAN = datetime.date(2026, 1, 2)


def _owner(make_tenant, make_user):
    tenant = make_tenant(name="Portfolios")
    owner = make_user("owner@example.com")
    Membership.objects.create(user=owner, tenant=tenant, role=Role.OWNER)
    return tenant, owner


def _brokerage(name="Fidelity"):
    org = Organization.objects.create(name=name)
    org.categories.add(Category.objects.get(kind="ORG", name="Brokerage"))
    return org


def _account_with_cash(org, cash="1000", nickname="Roth", reg="roth_ira"):
    acct = InvestmentAccount.objects.create(
        institution=org, nickname=nickname, registration=reg,
        currency=Currency.objects.get(code="USD"),
    )
    ensure_gl_account(acct)
    txn = InvestmentTransaction.objects.create(
        account=acct, txn_type=InvTxnType.OPENING, date=JAN, amount=D(cash))
    apply_transaction(txn, is_new=True)
    return acct


def _url(tenant, path=""):
    return f"/t/{tenant.schema_name}/investments/{path}"


def test_institution_summary_totals_and_ordering(make_tenant):
    with schema_context(make_tenant().schema_name):
        vg = _brokerage("Vanguard")
        _account_with_cash(vg, "5000")
        _account_with_cash(vg, "1000", nickname="Trad", reg="traditional_ira")
        _brokerage("Empty Co")  # no accounts — still listed with zero totals

        rows = institution_summary()
        by_name = {r["org"].name: r for r in rows}
        assert by_name["Vanguard"]["total_value"] == D("6000")
        assert by_name["Vanguard"]["account_count"] == 2
        assert by_name["Empty Co"]["total_value"] == D("0")
        assert by_name["Empty Co"]["account_count"] == 0
        assert rows[0]["org"].name == "Vanguard"  # most valuable first


def test_institutions_index_empty(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    resp = client.get(_url(tenant, "institutions/"))
    assert resp.status_code == 200
    assert "No institutions yet" in resp.content.decode()


def test_institutions_index_lists_brokerage(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        _account_with_cash(_brokerage("Vanguard"), "5000")
    client.force_login(owner)
    body = client.get(_url(tenant, "institutions/")).content.decode()
    assert "Vanguard" in body
    assert "Value by institution" in body  # the breakdown bars render


def test_nav_has_institutions_link(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    body = client.get(_url(tenant, "institutions/")).content.decode()
    assert "investments/institutions/" in body and "Institutions" in body


def test_add_brokerage_creates_tagged_org_and_opens_detail(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    resp = client.post(
        _url(tenant, "institutions/new/"),
        {"name": "Schwab", "city": "Westlake", "website": "https://schwab.com"},
    )
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        org = Organization.objects.get(name="Schwab")
        assert org.categories.filter(name="Brokerage").exists()
        assert org.primary_city == "Westlake"
        assert org.website == "https://schwab.com"
        assert resp.url.endswith(f"investments/institutions/{org.pk}/")


def test_add_brokerage_requires_name(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    resp = client.post(_url(tenant, "institutions/new/"), {"name": "  "})
    assert resp.status_code == 302  # bounced back to the list
    with schema_context(tenant.schema_name):
        assert not Organization.objects.exists()


def test_institution_detail_lists_accounts(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        org = _brokerage("Fidelity")
        _account_with_cash(org, "2500", nickname="My Roth")
    client.force_login(owner)
    body = client.get(_url(tenant, f"institutions/{org.pk}/")).content.decode()
    assert "My Roth" in body
    assert "Accounts at Fidelity" in body


def test_institution_detail_404_for_non_brokerage(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        org = Organization.objects.create(name="Some Employer")  # not tagged Brokerage
    client.force_login(owner)
    assert client.get(_url(tenant, f"institutions/{org.pk}/")).status_code == 404


def test_account_create_prefills_institution(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        org = _brokerage("Fidelity")
    client.force_login(owner)
    body = client.get(_url(tenant, f"accounts/new/?institution={org.pk}")).content.decode()
    assert f'value="{org.pk}" selected' in body  # the institution select is preselected


def test_edit_brokerage_updates_name_and_city(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        org = _brokerage("Fidellity")  # to be corrected
    client.force_login(owner)
    resp = client.post(
        _url(tenant, f"institutions/{org.pk}/edit/"),
        {"name": "Fidelity", "city": "Boston", "website": ""},
    )
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        org.refresh_from_db()
        assert org.name == "Fidelity"
        assert org.primary_city == "Boston"


def test_add_branch_to_brokerage(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        org = _brokerage("Fidelity")
    client.force_login(owner)
    resp = client.post(
        _url(tenant, f"institutions/{org.pk}/branches/new/"),
        {"branch_name": "Boston HQ", "branch_number": "001"},
    )
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        assert Branch.objects.filter(organization=org, name="Boston HQ", number="001").exists()
