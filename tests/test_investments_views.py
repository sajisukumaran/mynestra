"""Investments screens: dashboard, accounts list/detail, account + transaction capture, securities,
the member gate, and the Expert-only Accounting tab. Drives the authenticated tenant client."""

from django_tenants.utils import schema_context

from apps.investments.models import InvestmentAccount, InvestmentTransaction, Lot, Security
from apps.organizations.models import Organization
from apps.setup.models import Category
from apps.tenants.models import Membership, Role


def _owner(make_tenant, make_user, name="Portfolios", email="owner@example.com"):
    tenant = make_tenant(name=name)
    owner = make_user(email)
    Membership.objects.create(user=owner, tenant=tenant, role=Role.OWNER)
    return tenant, owner


def _brokerage(name="Fidelity"):
    org = Organization.objects.create(name=name)
    org.categories.add(Category.objects.get(kind="ORG", name="Brokerage"))
    return org


def _url(tenant, path=""):
    return f"/t/{tenant.schema_name}/investments/{path}"


def test_dashboard_and_list_render_empty(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    assert client.get(_url(tenant)).status_code == 200
    body = client.get(_url(tenant, "accounts/")).content.decode()
    assert "No accounts yet" in body


def test_non_member_is_denied(make_tenant, make_user, client):
    tenant, _o = _owner(make_tenant, make_user)
    outsider = make_user("outsider@example.com")
    client.force_login(outsider)
    assert client.get(_url(tenant)).status_code == 403


def test_account_create_form_renders_with_brokerage(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        _brokerage()
    client.force_login(owner)
    body = client.get(_url(tenant, "accounts/new/")).content.decode()
    assert "New account" in body and "Fidelity" in body


def test_create_account_inline_institution_and_opening_cash(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    resp = client.post(
        _url(tenant, "accounts/new/"),
        {
            "new_institution_name": "Vanguard",
            "new_institution_city": "Valley Forge",
            "registration": "roth_ira",
            "nickname": "My Roth",
            "currency": "USD",
            "is_active": "on",
            "opening_balance": "5000",
            "opening_date": "2026-01-02",
        },
    )
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        acct = InvestmentAccount.objects.get(nickname="My Roth")
        assert acct.registration == "roth_ira"
        assert acct.gl_account.parent.code == "1220"           # retirement group header
        assert acct.institution.categories.filter(name="Brokerage").exists()
        assert acct.cash_balance == __import__("decimal").Decimal("5000")


def test_add_buy_then_sell_via_views(make_tenant, make_user, client):
    from decimal import Decimal

    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        from apps.finance.models import Currency
        from apps.investments.services import ensure_gl_account
        acct = InvestmentAccount.objects.create(
            institution=_brokerage(), nickname="Taxable", registration="taxable_individual",
            currency=Currency.objects.get(code="USD"))
        ensure_gl_account(acct)
        sec = Security.objects.create(symbol="ACME", name="Acme",
                                      currency=Currency.objects.get(code="USD"))
        aid, sid = acct.pk, sec.pk
    client.force_login(owner)

    client.post(_url(tenant, f"accounts/{aid}/txns/new/"), {
        "txn_type": "opening", "date": "2026-01-02", "amount": "10000"})
    client.post(_url(tenant, f"accounts/{aid}/txns/new/"), {
        "txn_type": "buy", "date": "2026-01-05", "security": sid,
        "quantity": "10", "price": "50", "amount": "500", "fee": "0"})
    client.post(_url(tenant, f"accounts/{aid}/txns/new/"), {
        "txn_type": "sell", "date": "2026-03-01", "security": sid,
        "quantity": "4", "price": "70", "amount": "280", "fee": "0"})

    with schema_context(tenant.schema_name):
        assert Lot.objects.filter(account_id=aid, open=True).count() == 1
        sell = InvestmentTransaction.objects.get(account_id=aid, txn_type="sell")
        assert sell.realized_gain == Decimal("80")

    # Detail page shows the holding + drill-down works.
    body = client.get(_url(tenant, f"accounts/{aid}/")).content.decode()
    assert "ACME" in body and "Holdings" in body
    assert client.get(_url(tenant, f"accounts/{aid}/holdings/{sid}/")).status_code == 200


def test_security_create_and_price(make_tenant, make_user, client):
    from decimal import Decimal

    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    resp = client.post(_url(tenant, "securities/new/"), {
        "symbol": "VTI", "name": "Vanguard Total Stock", "kind": "etf",
        "asset_class": "equity", "currency": "USD", "is_active": "on"})
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        sec = Security.objects.get(symbol="VTI")
    client.post(_url(tenant, f"securities/{sec.pk}/price/"), {
        "price": "255.50", "as_of": "2026-06-01", "source": "Broker"})
    with schema_context(tenant.schema_name):
        sec.refresh_from_db()
        assert sec.latest_price == Decimal("255.50")
    body = client.get(_url(tenant, "securities/")).content.decode()
    assert "VTI" in body


def test_expert_accounting_tab_appears_in_expert_mode(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    tenant.accounting_mode = "expert"
    tenant.save(update_fields=["accounting_mode"])
    with schema_context(tenant.schema_name):
        _brokerage()
    client.force_login(owner)
    body = client.get(_url(tenant, "accounts/new/")).content.decode()
    assert "Accounting" in body and "ledger node" in body
