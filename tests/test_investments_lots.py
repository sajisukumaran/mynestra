"""Tax-lot engine: buys create lots, sells consume them (FIFO + specific), splits scale, return of
capital reduces basis, and edits/deletes replay correctly. Realized gains are lot-accurate."""

import datetime
from decimal import Decimal

import pytest
from django_tenants.utils import schema_context

from apps.finance.models import Currency
from apps.investments.exceptions import InsufficientShares
from apps.investments.models import (
    InvestmentAccount,
    InvestmentTransaction,
    InvTxnType,
    Lot,
    Security,
)
from apps.investments.services import apply_transaction, cost_basis, ensure_gl_account, holdings
from apps.organizations.models import Organization

D = Decimal
JAN = datetime.date(2026, 1, 2)


def _setup():
    org = Organization.objects.create(name="Broker")
    acct = InvestmentAccount.objects.create(
        institution=org, nickname="Taxable", registration="taxable_individual",
        currency=Currency.objects.get(code="USD"),
    )
    ensure_gl_account(acct)
    sec = Security.objects.create(
        symbol="ACME", name="Acme", currency=Currency.objects.get(code="USD"))
    return acct, sec


def _add(acct, ttype, date, *, security=None, qty="0", price="0", amount="0", fee="0", **extra):
    txn = InvestmentTransaction.objects.create(
        account=acct, txn_type=ttype, date=date, security=security,
        quantity=D(qty), price=D(price), amount=D(amount), fee=D(fee), **extra,
    )
    apply_transaction(txn, is_new=True)
    txn.refresh_from_db()
    return txn


def test_buy_creates_lot_with_capitalized_commission(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        _add(acct, InvTxnType.BUY, JAN, security=sec, qty="10", price="50", amount="500", fee="10")
        lot = Lot.objects.get(account=acct, security=sec)
        assert lot.remaining_quantity == D("10")
        assert lot.cost_basis == D("510")  # commission capitalized into basis
        assert cost_basis(acct) == D("510")


def test_fifo_partial_sell_realizes_gain_and_reduces_lot(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        _add(acct, InvTxnType.BUY, JAN, security=sec, qty="10", price="50", amount="500")
        sell = _add(acct, InvTxnType.SELL, datetime.date(2026, 3, 1),
                    security=sec, qty="4", price="70", amount="280")
        assert sell.realized_gain == D("80")  # proceeds 280 − cost 200
        lot = Lot.objects.get(account=acct, security=sec)
        assert lot.remaining_quantity == D("6")
        assert lot.cost_basis == D("300")


def test_fifo_sell_spans_multiple_lots(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        _add(acct, InvTxnType.BUY, JAN, security=sec, qty="10", price="10", amount="100")
        _add(acct, InvTxnType.BUY, datetime.date(2026, 2, 1),
             security=sec, qty="10", price="20", amount="200")
        sell = _add(acct, InvTxnType.SELL, datetime.date(2026, 3, 1),
                    security=sec, qty="15", price="30", amount="450")
        # FIFO: 10 @10 (cost 100) + 5 @20 (cost 100) = 200 cost; proceeds 450 → gain 250.
        assert sell.realized_gain == D("250")
        assert cost_basis(acct) == D("100")  # 5 @20 remaining


def test_full_sale_closes_lot(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        _add(acct, InvTxnType.BUY, JAN, security=sec, qty="5", price="10", amount="50")
        _add(acct, InvTxnType.SELL, datetime.date(2026, 2, 1),
             security=sec, qty="5", price="12", amount="60")
        assert not Lot.objects.filter(account=acct, open=True).exists()
        assert holdings(acct) == []


def test_oversell_raises(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        _add(acct, InvTxnType.BUY, JAN, security=sec, qty="5", price="10", amount="50")
        with pytest.raises(InsufficientShares):
            _add(acct, InvTxnType.SELL, datetime.date(2026, 2, 1),
                 security=sec, qty="10", price="12", amount="120")


def test_editing_a_buy_updates_a_later_sells_gain(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        buy = _add(acct, InvTxnType.BUY, JAN, security=sec, qty="10", price="50", amount="500")
        sell = _add(acct, InvTxnType.SELL, datetime.date(2026, 3, 1),
                    security=sec, qty="10", price="70", amount="700")
        assert sell.realized_gain == D("200")
        # Correct the purchase cost upward; the sale's realized gain must shrink after replay.
        buy.amount = D("600")
        buy.save()
        apply_transaction(buy, is_new=False)
        sell.refresh_from_db()
        assert sell.realized_gain == D("100")


def test_deleting_an_unsold_buy_removes_the_lot(make_tenant):
    from apps.investments.services import remove_transaction

    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        buy = _add(acct, InvTxnType.BUY, JAN, security=sec, qty="10", price="50", amount="500")
        assert cost_basis(acct) == D("500")
        remove_transaction(buy)
        assert cost_basis(acct) == D("0")
        assert not Lot.objects.filter(account=acct, open=True).exists()


def test_stock_split_scales_quantity_preserving_basis(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        _add(acct, InvTxnType.BUY, JAN, security=sec, qty="10", price="100", amount="1000")
        _add(acct, InvTxnType.SPLIT, datetime.date(2026, 2, 1), security=sec,
             split_ratio_new=D("2"), split_ratio_old=D("1"))
        lot = Lot.objects.get(account=acct, security=sec)
        assert lot.remaining_quantity == D("20")      # doubled
        assert lot.cost_basis == D("1000")            # total basis unchanged
        assert lot.per_share_cost == D("50")          # halved


def test_return_of_capital_reduces_basis_then_realizes_excess(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        _add(acct, InvTxnType.BUY, JAN, security=sec, qty="10", price="10", amount="100")
        # Within basis: reduces cost, no gain.
        roc1 = _add(acct, InvTxnType.RETURN_OF_CAPITAL, datetime.date(2026, 2, 1),
                    security=sec, amount="40")
        assert roc1.realized_gain == D("0")
        assert cost_basis(acct) == D("60")
        # Exceeds remaining basis (60): 60 reduces basis to 0, excess 20 is a realized gain.
        roc2 = _add(acct, InvTxnType.RETURN_OF_CAPITAL, datetime.date(2026, 3, 1),
                    security=sec, amount="80")
        assert roc2.realized_gain == D("20")
        assert cost_basis(acct) == D("0")


def test_dividend_reinvest_creates_a_lot(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        _add(acct, InvTxnType.BUY, JAN, security=sec, qty="10", price="50", amount="500")
        _add(acct, InvTxnType.DIVIDEND_REINVEST, datetime.date(2026, 2, 1),
             security=sec, qty="0.5", price="60", amount="30")
        assert cost_basis(acct) == D("530")  # 500 + 30 reinvested
        h = holdings(acct)[0]
        assert h.quantity == D("10.5")


def test_opening_holding_creates_lot_without_cash(make_tenant):
    from apps.investments.services import cash_balance

    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        _add(acct, InvTxnType.OPENING, JAN, security=sec, qty="8", price="25", amount="200")
        assert cost_basis(acct) == D("200")
        assert cash_balance(acct) == D("0")  # opening a holding brings in securities, not cash


def test_specific_lot_selection_by_source_buy(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        b1 = _add(acct, InvTxnType.BUY, JAN, security=sec, qty="10", price="10", amount="100")
        _add(acct, InvTxnType.BUY, datetime.date(2026, 2, 1),
             security=sec, qty="10", price="20", amount="200")
        # Sell 5 specifically from the cheaper first lot → cost 50, proceeds 150, gain 100.
        sell = InvestmentTransaction.objects.create(
            account=acct, txn_type=InvTxnType.SELL, date=datetime.date(2026, 3, 1),
            security=sec, quantity=D("5"), price=D("30"), amount=D("150"),
            cost_basis_method="specific", lot_selection=[{"buy_txn": b1.pk, "qty": "5"}],
        )
        apply_transaction(sell, is_new=True)
        sell.refresh_from_db()
        assert sell.realized_gain == D("100")
