"""Lot pooling (average-cost, for money-market / stable-value funds) + the account income rollup.

Pooling: a Security with track_lots=False folds every buy/reinvest into ONE average-cost lot
instead of a lot per dividend; sells draw at the blended cost; toggling the flag re-pools existing
lots. The `gl == cash + Σ open-lot cost` invariant holds throughout. Income: dividends (incl.
reinvested) + interest + cap-gain distributions, grouped by year received."""

import datetime
from decimal import Decimal

from django_tenants.utils import schema_context

from apps.finance.models import Currency
from apps.finance.services import account_balance
from apps.investments.models import InvestmentAccount, InvestmentTransaction, InvTxnType, Security
from apps.investments.services import (
    apply_transaction,
    cash_balance,
    cost_basis,
    ensure_gl_account,
    income_summary,
    repool_security,
    transfer_totals,
)
from apps.organizations.models import Organization
from apps.setup.models import Category
from apps.tenants.models import Membership, Role

D = Decimal
JAN = datetime.date(2022, 1, 3)
FEB = datetime.date(2022, 2, 28)
MAR = datetime.date(2022, 3, 31)
APR = datetime.date(2022, 4, 29)


def _owner(make_tenant, make_user):
    tenant = make_tenant(name="Portfolios")
    owner = make_user("owner@example.com")
    Membership.objects.create(user=owner, tenant=tenant, role=Role.OWNER)
    return tenant, owner


def _setup(track_lots=True, kind="stock"):
    org = Organization.objects.create(name="Broker")
    org.categories.add(Category.objects.get(kind="ORG", name="Brokerage"))
    acct = InvestmentAccount.objects.create(
        institution=org, nickname="Taxable", registration="taxable_individual",
        currency=Currency.objects.get(code="USD"))
    ensure_gl_account(acct)
    sec = Security.objects.create(
        symbol="MM", name="Money Market", kind=kind,
        currency=Currency.objects.get(code="USD"), track_lots=track_lots)
    return acct, sec


def _add(acct, ttype, date, **kw):
    fields = {"quantity": "0", "price": "0", "amount": "0", "fee": "0"}
    fields.update(kw)
    txn = InvestmentTransaction.objects.create(
        account=acct, txn_type=ttype, date=date,
        quantity=D(fields.pop("quantity")), price=D(fields.pop("price")),
        amount=D(fields.pop("amount")), fee=D(fields.pop("fee")), **fields,
    )
    apply_transaction(txn, is_new=True)
    txn.refresh_from_db()
    return txn


def _invariant(acct):
    return account_balance(acct.gl_account) == cash_balance(acct) + cost_basis(acct)


def _url(tenant, path=""):
    return f"/t/{tenant.schema_name}/investments/{path}"


# --- Pooling --------------------------------------------------------------------------------

def test_pooled_security_accumulates_into_one_lot(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup(track_lots=False, kind="money_market")
        _add(acct, InvTxnType.OPENING, FEB, security=sec, quantity="7000", price="1", amount="7000")
        _add(acct, InvTxnType.DIVIDEND_REINVEST, MAR, security=sec,
             quantity="0.06", price="1", amount="0.06")
        _add(acct, InvTxnType.DIVIDEND_REINVEST, APR, security=sec,
             quantity="0.05", price="1", amount="0.05")
        lots = list(acct.lots.filter(security=sec, open=True))
        assert len(lots) == 1                            # one pooled lot, not three
        assert lots[0].remaining_quantity == D("7000.11")
        assert lots[0].cost_basis == D("7000.11")
        assert _invariant(acct)


def test_tracked_security_keeps_separate_lots(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup(track_lots=True, kind="money_market")
        _add(acct, InvTxnType.OPENING, FEB, security=sec, quantity="7000", price="1", amount="7000")
        _add(acct, InvTxnType.DIVIDEND_REINVEST, MAR, security=sec,
             quantity="0.06", price="1", amount="0.06")
        assert acct.lots.filter(security=sec, open=True).count() == 2  # a lot per event
        assert _invariant(acct)


def test_pooled_sell_uses_average_cost(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup(track_lots=False, kind="mutual_fund")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")  # cash
        _add(acct, InvTxnType.BUY, JAN, security=sec, quantity="100", price="10", amount="1000")
        _add(acct, InvTxnType.BUY, FEB, security=sec, quantity="100", price="20", amount="2000")
        # pooled: 200 sh, cost 3000, average $15/sh
        sell = _add(acct, InvTxnType.SELL, MAR, security=sec,
                    quantity="100", price="18", amount="1800")
        assert sell.realized_gain == D("300")            # 1800 − 100×15 (average cost)
        lot = acct.lots.get(security=sec, open=True)
        assert lot.remaining_quantity == D("100")
        assert lot.cost_basis == D("1500")
        assert _invariant(acct)


def test_toggling_track_lots_off_repools_existing_lots(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup(track_lots=True, kind="money_market")
        _add(acct, InvTxnType.OPENING, FEB, security=sec, quantity="7000", price="1", amount="7000")
        _add(acct, InvTxnType.DIVIDEND_REINVEST, MAR, security=sec,
             quantity="0.06", price="1", amount="0.06")
        _add(acct, InvTxnType.DIVIDEND_REINVEST, APR, security=sec,
             quantity="0.05", price="1", amount="0.05")
        assert acct.lots.filter(security=sec, open=True).count() == 3

        sec.track_lots = False
        sec.save()
        repool_security(sec)

        lots = list(acct.lots.filter(security=sec, open=True))
        assert len(lots) == 1
        assert lots[0].cost_basis == D("7000.11")
        assert _invariant(acct)


def test_security_edit_toggle_off_repools_via_view(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        acct, sec = _setup(track_lots=True, kind="money_market")
        _add(acct, InvTxnType.OPENING, FEB, security=sec, quantity="7000", price="1", amount="7000")
        _add(acct, InvTxnType.DIVIDEND_REINVEST, MAR, security=sec,
             quantity="0.06", price="1", amount="0.06")
        assert acct.lots.filter(security=sec, open=True).count() == 2
    client.force_login(owner)
    resp = client.post(
        _url(tenant, f"securities/{sec.pk}/edit/"),
        {"symbol": "MM", "name": "Money Market", "kind": "money_market",
         "asset_class": "cash", "currency": "USD", "is_active": "on"},  # track_lots omitted → off
    )
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        sec.refresh_from_db()
        assert sec.track_lots is False
        assert acct.lots.filter(security=sec, open=True).count() == 1


# --- Income ---------------------------------------------------------------------------------

def test_income_summary_groups_by_year_with_total(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        _add(acct, InvTxnType.DIVIDEND, datetime.date(2024, 3, 1), security=sec, amount="100")
        _add(acct, InvTxnType.INTEREST, datetime.date(2024, 6, 1), amount="50")
        _add(acct, InvTxnType.CAP_GAIN_DIST, datetime.date(2023, 12, 1), amount="200")
        _add(acct, InvTxnType.DIVIDEND_REINVEST, datetime.date(2023, 5, 1), security=sec,
             quantity="10", price="1", amount="10")

        summary = income_summary(acct)
        assert summary["has_income"] is True
        assert summary["total"] == D("360")
        assert summary["by_year"] == [
            {"year": 2024, "total": D("150")},  # dividend 100 + interest 50
            {"year": 2023, "total": D("210")},  # cap-gain 200 + reinvested dividend 10
        ]


def test_income_summary_excludes_non_income(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        _add(acct, InvTxnType.OPENING, JAN, amount="5000")
        _add(acct, InvTxnType.BUY, JAN, security=sec, quantity="10", price="50", amount="500")
        summary = income_summary(acct)
        assert summary["has_income"] is False
        assert summary["total"] == D("0")


def test_account_detail_renders_income_collected(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        acct, sec = _setup()
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        _add(acct, InvTxnType.DIVIDEND, datetime.date(2024, 3, 1), security=sec, amount="125")
    client.force_login(owner)
    body = client.get(_url(tenant, f"accounts/{acct.pk}/")).content.decode()
    assert "Income collected" in body


# --- Transfer totals ------------------------------------------------------------------------

def test_transfer_totals_sum_in_and_out(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        InvestmentTransaction.objects.create(
            account=acct, txn_type=InvTxnType.TRANSFER_IN, date=JAN, amount=D("1000"))
        InvestmentTransaction.objects.create(
            account=acct, txn_type=InvTxnType.TRANSFER_IN, date=FEB, amount=D("500"))
        InvestmentTransaction.objects.create(
            account=acct, txn_type=InvTxnType.TRANSFER_OUT, date=MAR, amount=D("300"))
        # A contribution is money in but NOT a transfer — excluded.
        InvestmentTransaction.objects.create(
            account=acct, txn_type=InvTxnType.CONTRIBUTION, date=MAR, amount=D("9999"))

        totals = transfer_totals(acct)
        assert totals["transfer_in"] == D("1500")
        assert totals["transfer_out"] == D("300")


def test_transfer_totals_zero_when_none(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        totals = transfer_totals(acct)
        assert totals["transfer_in"] == D("0")
        assert totals["transfer_out"] == D("0")


def test_account_detail_renders_transfer_totals(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        acct, sec = _setup()
        InvestmentTransaction.objects.create(
            account=acct, txn_type=InvTxnType.TRANSFER_IN, date=JAN, amount=D("1000"))
        InvestmentTransaction.objects.create(
            account=acct, txn_type=InvTxnType.TRANSFER_OUT, date=MAR, amount=D("300"))
    client.force_login(owner)
    body = client.get(_url(tenant, f"accounts/{acct.pk}/")).content.decode()
    assert "transferred in" in body
    assert "transferred out" in body
