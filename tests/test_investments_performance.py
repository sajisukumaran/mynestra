"""Per-instrument performance report: the `security_performance` aggregation (bought/sold/qty, cost,
fees, dividends, interest, amount sold, realized, price, gain, total return) + totals footer, that
fully-sold instruments still appear, and that the Performance tab renders on both the account and
institution pages."""

import datetime
from decimal import Decimal

from django_tenants.utils import schema_context

from apps.finance.models import Currency
from apps.investments.models import (
    InvestmentAccount,
    InvestmentTransaction,
    InvTxnType,
    Security,
    SecurityPrice,
)
from apps.investments.services import apply_transaction, ensure_gl_account, security_performance
from apps.organizations.models import Organization
from apps.setup.models import Category
from apps.tenants.models import Membership, Role

D = Decimal
JAN = datetime.date(2024, 1, 2)
FEB = datetime.date(2024, 2, 1)
MAR = datetime.date(2024, 3, 1)


def _owner(make_tenant, make_user):
    tenant = make_tenant(name="Portfolios")
    owner = make_user("owner@example.com")
    Membership.objects.create(user=owner, tenant=tenant, role=Role.OWNER)
    return tenant, owner


def _brokerage(name="Fidelity"):
    org = Organization.objects.create(name=name)
    org.categories.add(Category.objects.get(kind="ORG", name="Brokerage"))
    return org


def _setup():
    usd = Currency.objects.get(code="USD")
    acct = InvestmentAccount.objects.create(
        institution=_brokerage(), nickname="Taxable", registration="taxable_individual",
        currency=usd)
    ensure_gl_account(acct)
    aapl = Security.objects.create(symbol="AAPL", name="Apple", currency=usd)
    msft = Security.objects.create(symbol="MSFT", name="Microsoft", currency=usd)
    bondx = Security.objects.create(symbol="BONDX", name="Bond", kind="bond",
                                    asset_class="fixed_income", currency=usd)
    return acct, aapl, msft, bondx


def _add(acct, ttype, date, **kw):
    fields = {"quantity": "0", "price": "0", "amount": "0", "fee": "0"}
    fields.update(kw)
    txn = InvestmentTransaction.objects.create(
        account=acct, txn_type=ttype, date=date,
        quantity=D(fields.pop("quantity")), price=D(fields.pop("price")),
        amount=D(fields.pop("amount")), fee=D(fields.pop("fee")), **fields,
    )
    apply_transaction(txn, is_new=True)
    return txn


def _url(tenant, path=""):
    return f"/t/{tenant.schema_name}/investments/{path}"


def _scenario(acct, aapl, msft, bondx):
    _add(acct, InvTxnType.OPENING, JAN, amount="5000")
    _add(acct, InvTxnType.BUY, JAN, security=aapl, quantity="100", price="10", amount="1000",
         fee="5")                                                    # cost basis 1005
    _add(acct, InvTxnType.DIVIDEND, FEB, security=aapl, amount="30")
    _add(acct, InvTxnType.SELL, MAR, security=aapl, quantity="40", price="15", amount="600")
    _add(acct, InvTxnType.BUY, JAN, security=msft, quantity="10", price="50", amount="500")
    _add(acct, InvTxnType.SELL, MAR, security=msft, quantity="10", price="60", amount="600")
    _add(acct, InvTxnType.INTEREST, MAR, security=bondx, amount="40")
    SecurityPrice.objects.create(security=aapl, as_of=MAR, price=D("18"))


def test_performance_row_columns(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, aapl, msft, bondx = _setup()
        _scenario(acct, aapl, msft, bondx)

        rows = {r.security.symbol: r for r in security_performance(acct)["rows"]}
        a = rows["AAPL"]
        assert a.qty_bought == D("100")
        assert a.qty_sold == D("40")
        assert a.current_qty == D("60")
        assert a.cost_basis == D("603")     # 1005 − 40×10.05
        assert a.fees == D("5")
        assert a.dividends == D("30")
        assert a.amount_sold == D("600")
        assert a.realized == D("198")       # 600 − 402
        assert a.price == D("18")
        assert a.market_value == D("1080")  # 60 × 18
        assert a.unrealized == D("477")     # 1080 − 603
        assert a.gain == D("675")           # realized 198 + unrealized 477
        assert a.income == D("30")
        assert a.total_return == D("705")   # gain 675 + income 30


def test_fully_sold_instrument_still_appears(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, aapl, msft, bondx = _setup()
        _scenario(acct, aapl, msft, bondx)
        rows = {r.security.symbol: r for r in security_performance(acct)["rows"]}
        m = rows["MSFT"]                     # bought + fully sold → no current position
        assert m.current_qty == D("0")
        assert m.cost_basis == D("0")
        assert m.qty_sold == D("10")
        assert m.amount_sold == D("600")
        assert m.realized == D("100")        # 600 − 500
        assert m.gain == D("100")            # realized only (no unrealized)
        assert m.price is None


def test_income_only_instrument_and_totals(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, aapl, msft, bondx = _setup()
        _scenario(acct, aapl, msft, bondx)
        report = security_performance(acct)
        rows = {r.security.symbol: r for r in report["rows"]}
        b = rows["BONDX"]                    # interest, no position
        assert b.interest == D("40")
        assert b.income == D("40")
        assert b.total_return == D("40")
        assert b.current_qty == D("0")

        totals = report["totals"]
        assert totals["dividends"] == D("30")
        assert totals["interest"] == D("40")
        assert totals["income"] == D("70")
        assert totals["realized"] == D("298")      # AAPL 198 + MSFT 100
        assert totals["gain"] == D("775")          # AAPL 675 + MSFT 100
        assert totals["total_return"] == D("845")  # gain 775 + income 70


def test_account_detail_renders_performance_tab(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        acct, aapl, msft, bondx = _setup()
        _scenario(acct, aapl, msft, bondx)
    client.force_login(owner)
    body = client.get(_url(tenant, f"accounts/{acct.pk}/")).content.decode()
    assert "Performance" in body
    assert "Total return" in body
    assert "AAPL" in body


def test_institution_detail_renders_performance_tab(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        acct, aapl, msft, bondx = _setup()
        _scenario(acct, aapl, msft, bondx)
        org_pk = acct.institution_id
    client.force_login(owner)
    body = client.get(_url(tenant, f"institutions/{org_pk}/")).content.decode()
    assert "Performance" in body
    assert "Total return" in body
    assert acct.nickname in body
