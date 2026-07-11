"""Phase IP5 (options) — full-lifecycle equity options: buy/sell to open/close, expiry, and the
two-security exercise/assignment that rolls the option's premium basis into (or out of) the
underlying's tax lots. Covers the ×100 multiplier, the "ACQUIRE posts nothing / DISPOSE realizes
gain" split, naked exercise (opens a short underlying lot), replay/delete determinism of the
two-security transaction, and the capture views. `_inv` (gl == cash + Σ open-lot cost) is asserted
throughout."""

import datetime
from decimal import Decimal

import pytest
from django_tenants.utils import schema_context

from apps.finance.models import Currency
from apps.finance.services import account_balance
from apps.investments.exceptions import InsufficientShares
from apps.investments.models import (
    AssetClass,
    InvestmentAccount,
    InvestmentTransaction,
    InvTxnType,
    Lot,
    OptionRight,
    Security,
    SecurityKind,
)
from apps.investments.services import (
    apply_transaction,
    cash_balance,
    cost_basis,
    ensure_gl_account,
    remove_transaction,
)
from apps.organizations.models import Organization
from apps.setup.models import Category
from apps.tenants.models import Membership, Role

D = Decimal
JAN = datetime.date(2026, 1, 2)
FEB = datetime.date(2026, 2, 2)
MAR = datetime.date(2026, 3, 2)
APR = datetime.date(2026, 4, 2)


def _account(org=None):
    acct = InvestmentAccount.objects.create(
        institution=org or Organization.objects.create(name="Broker"),
        nickname="Taxable", registration="taxable_individual",
        currency=Currency.objects.get(code="USD"),
    )
    ensure_gl_account(acct)
    return acct


def _sec(symbol, name=None):
    return Security.objects.create(
        symbol=symbol, name=name or symbol, currency=Currency.objects.get(code="USD")
    )


def _opt(underlying, right, strike, mult="100"):
    return Security.objects.create(
        symbol="", name=f"{underlying.symbol} {right}", kind=SecurityKind.OPTION,
        asset_class=AssetClass.DERIVATIVE, currency=Currency.objects.get(code="USD"),
        underlying=underlying, option_right=right, strike=D(strike),
        expiration=datetime.date(2026, 7, 19), multiplier=D(mult),
    )


def _add(acct, ttype, date, *, security=None, qty="0", price="0", amount="0", fee="0", **extra):
    txn = InvestmentTransaction.objects.create(
        account=acct, txn_type=ttype, date=date, security=security,
        quantity=D(qty), price=D(price), amount=D(amount), fee=D(fee), **extra,
    )
    apply_transaction(txn, is_new=True)
    txn.refresh_from_db()
    return txn


def _inv(acct) -> bool:
    acct.refresh_from_db()
    return account_balance(acct.gl_account) == cash_balance(acct) + cost_basis(acct)


def _open(acct, security):
    return list(Lot.objects.filter(account=acct, security=security, open=True))


# --- Open / close ----------------------------------------------------------------------------

def test_buy_open_creates_long_option_lot_and_posts_nothing(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        aapl = _sec("AAPL")
        c = _opt(aapl, OptionRight.CALL, "250")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        gl_before = account_balance(acct.gl_account)
        # 1 contract × 100 = 100 shares-equiv, premium $5/sh = $500.
        t = _add(acct, InvTxnType.OPT_BUY_OPEN, FEB, security=c, qty="100", price="5", amount="500")
        lots = _open(acct, c)
        assert len(lots) == 1 and lots[0].remaining_quantity == D("100")
        assert lots[0].cost_basis == D("500")
        assert cash_balance(acct) == D("9500")
        assert account_balance(acct.gl_account) == gl_before  # posts nothing
        assert t.journal_entry_id is None
        assert _inv(acct)


def test_sell_close_long_option_realizes_gain(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        c = _opt(_sec("AAPL"), OptionRight.CALL, "250")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        _add(acct, InvTxnType.OPT_BUY_OPEN, FEB, security=c, qty="100", price="5", amount="500")
        close = _add(acct, InvTxnType.OPT_SELL_CLOSE, MAR, security=c, qty="100", price="8",
                     amount="800")
        assert close.realized_gain == D("300")            # 800 − 500
        assert not _open(acct, c)
        assert _inv(acct)


def test_write_option_creates_negative_lot(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        p = _opt(_sec("AAPL"), OptionRight.PUT, "240")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        t = _add(acct, InvTxnType.OPT_SELL_OPEN, FEB, security=p, qty="100", price="3",
                 amount="300")
        lots = _open(acct, p)
        assert len(lots) == 1 and lots[0].remaining_quantity == D("-100")
        assert lots[0].cost_basis == D("-300")            # credit basis = premium received
        assert cash_balance(acct) == D("10300")
        assert t.journal_entry_id is None
        assert cost_basis(acct) == D("-300")              # negative basis counted
        assert _inv(acct)


def test_buy_close_written_option_realizes_gain(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        p = _opt(_sec("AAPL"), OptionRight.PUT, "240")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        _add(acct, InvTxnType.OPT_SELL_OPEN, FEB, security=p, qty="100", price="3", amount="300")
        close = _add(acct, InvTxnType.OPT_BUY_CLOSE, MAR, security=p, qty="100", price="2",
                     amount="200")
        assert close.realized_gain == D("100")            # 300 premium − 200 buy-back
        assert not _open(acct, p)
        assert _inv(acct)


# --- Expiry ----------------------------------------------------------------------------------

def test_long_option_expires_full_loss(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        c = _opt(_sec("AAPL"), OptionRight.CALL, "250")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        _add(acct, InvTxnType.OPT_BUY_OPEN, FEB, security=c, qty="100", price="5", amount="500")
        exp = _add(acct, InvTxnType.OPT_EXPIRE, MAR, security=c, qty="100")
        assert exp.realized_gain == D("-500")             # whole premium written off
        assert not _open(acct, c)
        assert _inv(acct)


def test_written_option_expires_full_gain(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        p = _opt(_sec("AAPL"), OptionRight.PUT, "240")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        _add(acct, InvTxnType.OPT_SELL_OPEN, FEB, security=p, qty="100", price="3", amount="300")
        exp = _add(acct, InvTxnType.OPT_EXPIRE, MAR, security=p, qty="100")
        assert exp.realized_gain == D("300")              # premium kept
        assert not _open(acct, p)
        assert _inv(acct)


# --- Exercise / assignment (two-security) ----------------------------------------------------

def test_long_call_exercise_rolls_basis_into_stock(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        mmm = _sec("MMM")
        c = _opt(mmm, OptionRight.CALL, "50")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        _add(acct, InvTxnType.OPT_BUY_OPEN, FEB, security=c, qty="100", price="3", amount="300")
        ex = _add(acct, InvTxnType.OPT_EXERCISE, MAR, security=c, qty="100", amount="5000")
        assert not _open(acct, c)                          # option consumed
        stock = _open(acct, mmm)
        assert len(stock) == 1 and stock[0].remaining_quantity == D("100")
        assert stock[0].cost_basis == D("5300")            # strike 5000 + premium 300
        assert ex.journal_entry_id is None                 # ACQUIRE posts nothing
        assert cash_balance(acct) == D("4700")             # 10000 − 300 − 5000
        assert _inv(acct)


def test_long_put_exercise_disposes_held_stock(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        xyz = _sec("XYZ")
        p = _opt(xyz, OptionRight.PUT, "50")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        _add(acct, InvTxnType.BUY, FEB, security=xyz, qty="100", price="40", amount="4000")
        _add(acct, InvTxnType.OPT_BUY_OPEN, FEB, security=p, qty="100", price="3", amount="300")
        ex = _add(acct, InvTxnType.OPT_EXERCISE, MAR, security=p, qty="100", amount="5000")
        assert not _open(acct, xyz)                        # stock sold at strike
        assert ex.realized_gain == D("700")                # (5000 − 300) − 4000
        assert _inv(acct)


def test_covered_call_assignment_sells_underlying(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        xyz = _sec("XYZ")
        call = _opt(xyz, OptionRight.CALL, "50")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        _add(acct, InvTxnType.BUY, FEB, security=xyz, qty="100", price="40", amount="4000")
        _add(acct, InvTxnType.OPT_SELL_OPEN, FEB, security=call, qty="100", price="2", amount="200")
        asg = _add(acct, InvTxnType.OPT_ASSIGN, MAR, security=call, qty="100", amount="5000")
        assert not _open(acct, xyz) and not _open(acct, call)
        assert asg.realized_gain == D("1200")              # (5000 + 200) − 4000
        assert _inv(acct)


def test_short_put_assignment_buys_underlying(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        ko = _sec("KO")
        put = _opt(ko, OptionRight.PUT, "30")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        _add(acct, InvTxnType.OPT_SELL_OPEN, FEB, security=put, qty="100", price="4", amount="400")
        asg = _add(acct, InvTxnType.OPT_ASSIGN, MAR, security=put, qty="100", amount="3000")
        stock = _open(acct, ko)
        assert len(stock) == 1 and stock[0].cost_basis == D("2600")  # strike 3000 − premium 400
        assert asg.journal_entry_id is None                # ACQUIRE posts nothing
        assert _inv(acct)


def test_naked_put_exercise_opens_short_underlying(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        nvda = _sec("NVDA")
        p = _opt(nvda, OptionRight.PUT, "30")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        _add(acct, InvTxnType.OPT_BUY_OPEN, FEB, security=p, qty="100", price="3", amount="300")
        _add(acct, InvTxnType.OPT_EXERCISE, MAR, security=p, qty="100", amount="3000")
        short = _open(acct, nvda)
        assert len(short) == 1 and short[0].remaining_quantity == D("-100")
        assert short[0].cost_basis == D("-2700")           # −(strike 3000 − premium 300)
        assert _inv(acct)


def test_partial_exercise_leaves_remaining_option(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        mmm = _sec("MMM")
        c = _opt(mmm, OptionRight.CALL, "50")
        _add(acct, InvTxnType.OPENING, JAN, amount="20000")
        # 3 contracts (300 shares-equiv) at $3/sh premium = $900 basis.
        _add(acct, InvTxnType.OPT_BUY_OPEN, FEB, security=c, qty="300", price="3", amount="900")
        _add(acct, InvTxnType.OPT_EXERCISE, MAR, security=c, qty="100", amount="5000")  # 1 contract
        opt_lots = _open(acct, c)
        assert len(opt_lots) == 1 and opt_lots[0].remaining_quantity == D("200")
        assert opt_lots[0].cost_basis == D("600")          # 900 × 200/300
        stock = _open(acct, mmm)
        assert stock[0].cost_basis == D("5300")            # 5000 + 300 (rolled premium)
        assert _inv(acct)


# --- Replay / delete determinism -------------------------------------------------------------

def test_exercise_replays_on_buy_open_edit(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        mmm = _sec("MMM")
        c = _opt(mmm, OptionRight.CALL, "50")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        buy = _add(acct, InvTxnType.OPT_BUY_OPEN, FEB, security=c, qty="100", price="3",
                   amount="300")
        _add(acct, InvTxnType.OPT_EXERCISE, MAR, security=c, qty="100", amount="5000")

        buy.amount = D("500")                              # premium 300 → 500
        buy.price = D("5")
        buy.save()
        apply_transaction(buy, is_new=False)

        assert _open(acct, mmm)[0].cost_basis == D("5500")  # 5000 + 500 rolled premium
        assert _inv(acct)


def test_delete_exercise_restores_option_and_unwinds_stock(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        mmm = _sec("MMM")
        c = _opt(mmm, OptionRight.CALL, "50")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        _add(acct, InvTxnType.OPT_BUY_OPEN, FEB, security=c, qty="100", price="3", amount="300")
        ex = _add(acct, InvTxnType.OPT_EXERCISE, MAR, security=c, qty="100", amount="5000")

        remove_transaction(ex)

        opt_lots = _open(acct, c)
        assert len(opt_lots) == 1 and opt_lots[0].remaining_quantity == D("100")  # restored
        assert opt_lots[0].cost_basis == D("300")
        assert not _open(acct, mmm)                        # stock lot unwound
        assert _inv(acct)


def test_delete_depended_on_buy_open_rolls_back(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        c = _opt(_sec("AAPL"), OptionRight.CALL, "250")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        buy = _add(acct, InvTxnType.OPT_BUY_OPEN, FEB, security=c, qty="100", price="5",
                   amount="500")
        _add(acct, InvTxnType.OPT_SELL_CLOSE, MAR, security=c, qty="100", price="8", amount="800")

        # Deleting the open leg leaves the close with nothing to consume → replay raises + reverts.
        with pytest.raises(InsufficientShares):
            remove_transaction(buy)
        assert InvestmentTransaction.objects.filter(pk=buy.pk).exists()  # delete rolled back
        assert _inv(acct)


def test_invariant_holds_across_all_option_types(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        xyz = _sec("XYZ")
        call = _opt(xyz, OptionRight.CALL, "50")
        put = _opt(xyz, OptionRight.PUT, "40")
        _add(acct, InvTxnType.OPENING, datetime.date(2026, 1, 1), amount="50000")
        _add(acct, InvTxnType.BUY, datetime.date(2026, 1, 3), security=xyz, qty="200", price="45",
             amount="9000")
        _add(acct, InvTxnType.OPT_BUY_OPEN, datetime.date(2026, 1, 5), security=call, qty="100",
             price="3", amount="300")
        _add(acct, InvTxnType.OPT_SELL_CLOSE, datetime.date(2026, 1, 7), security=call, qty="100",
             price="4", amount="400")
        _add(acct, InvTxnType.OPT_SELL_OPEN, datetime.date(2026, 1, 9), security=put, qty="100",
             price="2", amount="200")
        _add(acct, InvTxnType.OPT_EXPIRE, datetime.date(2026, 1, 12), security=put, qty="100")
        assert _inv(acct)


# --- Capture views ---------------------------------------------------------------------------

def _owner(make_tenant, make_user, name="Bourse", email="owner@example.com"):
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


def test_option_buy_open_via_view_expands_contracts(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        acct = _account(org=_brokerage())
        c = _opt(_sec("AAPL"), OptionRight.CALL, "250")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        aid, cid = acct.pk, c.pk
    client.force_login(owner)
    resp = client.post(_url(tenant, f"accounts/{aid}/txns/new/"), {
        "txn_type": "opt_buy_open", "date": "2026-02-02", "security": cid,
        "contracts": "2", "price": "5"})
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        c = Security.objects.get(pk=cid)
        lots = _open(acct, c)
        assert len(lots) == 1 and lots[0].remaining_quantity == D("200")  # 2 × 100
        assert lots[0].cost_basis == D("1000")             # 5 × 200
        assert _inv(acct)


def test_option_exercise_via_view(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        acct = _account(org=_brokerage())
        mmm = _sec("MMM")
        c = _opt(mmm, OptionRight.CALL, "50")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        _add(acct, InvTxnType.OPT_BUY_OPEN, FEB, security=c, qty="100", price="3", amount="300")
        aid, cid, mid = acct.pk, c.pk, mmm.pk
    client.force_login(owner)
    resp = client.post(_url(tenant, f"accounts/{aid}/txns/new/"), {
        "txn_type": "opt_exercise", "date": "2026-03-02", "security": cid, "contracts": "1"})
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        stock = _open(acct, Security.objects.get(pk=mid))
        assert len(stock) == 1 and stock[0].cost_basis == D("5300")  # 5000 strike + 300 premium
        assert _inv(acct)
