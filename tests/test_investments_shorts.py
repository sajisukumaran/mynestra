"""Phase IP5 (leverage) — short selling, margin borrowing, and the leverage-cost transactions
(margin interest, substitute dividends on a short). Covers the negative-lot engine (open a short,
cover FIFO/specific, partial cover), the "posts nothing on open / realized gain on cover" rule, the
margin = negative-cash behavior, the two dedicated expense postings, full-replay correctness, the
read models surfacing shorts, and the capture views. Mirrors the sibling suites' idioms. The core
invariant `gl == cash + Σ open-lot cost` is asserted throughout via `_inv`."""

import datetime
from decimal import Decimal

import pytest
from django_tenants.utils import schema_context

from apps.finance.models import Account, Currency
from apps.finance.services import account_balance
from apps.investments.exceptions import InsufficientShares
from apps.investments.models import (
    InvestmentAccount,
    InvestmentTransaction,
    InvTxnType,
    Lot,
    Security,
    SecurityPrice,
)
from apps.investments.services import (
    allocation,
    apply_transaction,
    cash_balance,
    cost_basis,
    ensure_gl_account,
    holdings,
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


# --- Service-level helpers (inside schema_context) -------------------------------------------

def _account(nickname="Taxable", registration="taxable_individual", org=None):
    acct = InvestmentAccount.objects.create(
        institution=org or Organization.objects.create(name="Broker"),
        nickname=nickname, registration=registration,
        currency=Currency.objects.get(code="USD"),
    )
    ensure_gl_account(acct)
    return acct


def _sec(symbol, name=None):
    return Security.objects.create(
        symbol=symbol, name=name or symbol, currency=Currency.objects.get(code="USD")
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
    """The core invariant: the postable GL node equals settlement cash + Σ open-lot cost basis."""
    acct.refresh_from_db()
    return account_balance(acct.gl_account) == cash_balance(acct) + cost_basis(acct)


def _open(acct, security):
    return list(Lot.objects.filter(account=acct, security=security, open=True))


def _bal(system_key):
    return account_balance(Account.objects.get(system_key=system_key))


# --- Short: open ------------------------------------------------------------------------------

def test_sell_short_creates_negative_lot_and_posts_nothing(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        z = _sec("ZZZ")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        gl_before = account_balance(acct.gl_account)

        short = _add(acct, InvTxnType.SELL_SHORT, FEB, security=z,
                     qty="100", price="50", amount="5000")

        lots = _open(acct, z)
        assert len(lots) == 1
        assert lots[0].remaining_quantity == D("-100")
        assert lots[0].cost_basis == D("-5000")           # credit basis = proceeds received
        assert cash_balance(acct) == D("15000")           # proceeds came in
        assert cost_basis(acct) == D("-5000")
        assert account_balance(acct.gl_account) == gl_before  # cost-neutral, nothing posts
        assert short.journal_entry_id is None
        assert _inv(acct)


# --- Short: cover -----------------------------------------------------------------------------

def test_cover_at_gain_credits_realized_gain(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        z = _sec("ZZZ")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        _add(acct, InvTxnType.SELL_SHORT, FEB, security=z, qty="100", price="50", amount="5000")
        cover = _add(acct, InvTxnType.BUY_TO_COVER, MAR, security=z,
                     qty="100", price="30", amount="3000")

        assert cover.realized_gain == D("2000")           # 5000 proceeds − 3000 cost
        assert not _open(acct, z)                          # short closed
        assert cash_balance(acct) == D("12000")            # 10000 + 5000 − 3000
        assert cost_basis(acct) == D("0")
        assert _bal("realized_capital_gain") == D("2000")  # credited
        assert _inv(acct)


def test_cover_at_loss_debits_realized_gain(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        z = _sec("ZZZ")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        _add(acct, InvTxnType.SELL_SHORT, FEB, security=z, qty="100", price="50", amount="5000")
        cover = _add(acct, InvTxnType.BUY_TO_COVER, MAR, security=z,
                     qty="100", price="70", amount="7000")

        assert cover.realized_gain == D("-2000")
        assert cash_balance(acct) == D("8000")             # 10000 + 5000 − 7000
        assert _bal("realized_capital_gain") == D("-2000")  # a loss (net debit)
        assert _inv(acct)


def test_partial_cover_keeps_short_open(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        z = _sec("ZZZ")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        _add(acct, InvTxnType.SELL_SHORT, FEB, security=z, qty="100", price="50", amount="5000")
        cover = _add(acct, InvTxnType.BUY_TO_COVER, MAR, security=z,
                     qty="40", price="30", amount="1200")

        assert cover.realized_gain == D("800")             # 2000 proceeds released − 1200 cost
        lots = _open(acct, z)
        assert len(lots) == 1 and lots[0].remaining_quantity == D("-60")
        assert lots[0].cost_basis == D("-3000")            # 5000 × 60/100
        assert cost_basis(acct) == D("-3000")
        assert _inv(acct)


def test_cover_more_than_short_raises(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        z = _sec("ZZZ")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        _add(acct, InvTxnType.SELL_SHORT, FEB, security=z, qty="100", price="50", amount="5000")
        with pytest.raises(InsufficientShares):
            _add(acct, InvTxnType.BUY_TO_COVER, MAR, security=z,
                 qty="150", price="30", amount="4500")


def test_fifo_cover_takes_oldest_short_first(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        z = _sec("ZZZ")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        _add(acct, InvTxnType.SELL_SHORT, FEB, security=z, qty="100", price="50", amount="5000")
        _add(acct, InvTxnType.SELL_SHORT, MAR, security=z, qty="100", price="60", amount="6000")
        # Cover 100 → drains the older (Feb, $50) short first, closing it; the Mar short survives.
        _add(acct, InvTxnType.BUY_TO_COVER, APR, security=z, qty="100", price="40", amount="4000")

        lots = _open(acct, z)
        assert len(lots) == 1
        assert lots[0].acquired_date == MAR and lots[0].cost_basis == D("-6000")
        assert _inv(acct)


def test_specific_cover_by_source_txn(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        z = _sec("ZZZ")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        _add(acct, InvTxnType.SELL_SHORT, FEB, security=z, qty="100", price="50", amount="5000")
        mar_short = _add(acct, InvTxnType.SELL_SHORT, MAR, security=z,
                         qty="100", price="60", amount="6000")
        # Explicitly cover the Mar short, leaving the older Feb short open.
        _add(acct, InvTxnType.BUY_TO_COVER, APR, security=z, qty="100", price="40", amount="4000",
             cost_basis_method="specific", lot_selection=[{"buy_txn": mar_short.pk, "qty": "100"}])

        lots = _open(acct, z)
        assert len(lots) == 1
        assert lots[0].acquired_date == FEB and lots[0].cost_basis == D("-5000")
        assert _inv(acct)


# --- Margin -----------------------------------------------------------------------------------

def test_buy_on_margin_allows_negative_cash(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        z = _sec("ZZZ")
        _add(acct, InvTxnType.OPENING, JAN, amount="1000")
        _add(acct, InvTxnType.BUY, FEB, security=z, qty="100", price="50", amount="5000", fee="10")

        assert cash_balance(acct) == D("-4010")            # bought beyond cash on hand
        assert cost_basis(acct) == D("5010")               # commission capitalized
        assert account_balance(acct.gl_account) == D("1000")  # unchanged from opening
        assert _inv(acct)


def test_margin_interest_expenses_interest_expense(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        _add(acct, InvTxnType.OPENING, JAN, amount="1000")
        mi = _add(acct, InvTxnType.MARGIN_INTEREST, FEB, amount="40")

        assert mi.security_id is None                      # account-level, no security
        assert cash_balance(acct) == D("960")
        assert _bal("interest_expense") == D("40")         # 5860 debited
        assert _inv(acct)


def test_div_paid_short_expenses_substitute_dividend(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        z = _sec("ZZZ")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        _add(acct, InvTxnType.SELL_SHORT, FEB, security=z, qty="100", price="50", amount="5000")
        _add(acct, InvTxnType.DIV_PAID_SHORT, MAR, security=z, amount="30")

        assert cash_balance(acct) == D("14970")            # 10000 + 5000 − 30
        assert _bal("substitute_dividend_expense") == D("30")  # 5880 debited
        assert _inv(acct)


# --- Replay / delete --------------------------------------------------------------------------

def test_short_replay_on_edit_updates_cover_gain(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        z = _sec("ZZZ")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        short = _add(acct, InvTxnType.SELL_SHORT, FEB, security=z,
                     qty="100", price="50", amount="5000")
        _add(acct, InvTxnType.BUY_TO_COVER, MAR, security=z, qty="100", price="30", amount="3000")

        # Raise the short proceeds → the later cover's realized gain must re-flow and re-post.
        short.amount = D("5500")
        short.price = D("55")
        short.save()
        apply_transaction(short, is_new=False)

        cover = InvestmentTransaction.objects.get(txn_type="buy_to_cover")
        assert cover.realized_gain == D("2500")            # 5500 − 3000
        assert _bal("realized_capital_gain") == D("2500")
        assert _inv(acct)


def test_delete_cover_reopens_short(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        z = _sec("ZZZ")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        _add(acct, InvTxnType.SELL_SHORT, FEB, security=z, qty="100", price="50", amount="5000")
        cover = _add(acct, InvTxnType.BUY_TO_COVER, MAR, security=z,
                     qty="100", price="30", amount="3000")

        remove_transaction(cover)

        lots = _open(acct, z)
        assert len(lots) == 1 and lots[0].remaining_quantity == D("-100")
        assert lots[0].cost_basis == D("-5000")            # short reopened cleanly
        assert cash_balance(acct) == D("15000")
        assert _bal("realized_capital_gain") == D("0")     # cover entry reversed
        assert _inv(acct)


# --- Read models ------------------------------------------------------------------------------

def test_holdings_surfaces_short_with_correct_pct_sign(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        z = _sec("ZZZ")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        _add(acct, InvTxnType.SELL_SHORT, FEB, security=z, qty="100", price="50", amount="5000")
        SecurityPrice.objects.create(security=z, as_of=MAR, price=D("40"))  # price fell → wins

        rows = {h.security.symbol: h for h in holdings(acct)}
        h = rows["ZZZ"]
        assert h.quantity == D("-100")
        assert h.market_value == D("-4000")                # −100 × 40
        assert h.cost_basis == D("-5000")
        assert h.unrealized_gain == D("1000")              # −4000 − (−5000)
        assert h.unrealized_pct == D("20")                 # 1000 / |−5000| × 100


def test_allocation_excludes_shorts(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        z = _sec("ZZZ")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        _add(acct, InvTxnType.SELL_SHORT, FEB, security=z, qty="100", price="50", amount="5000")
        SecurityPrice.objects.create(security=z, as_of=MAR, price=D("40"))

        slices = allocation([acct], by="asset_class")
        assert all(s.value > 0 for s in slices)            # no negative arc
        assert "Equity" not in [s.label for s in slices]   # the only equity position is short
        assert [s.value for s in slices] == [D("15000")]   # only the cash slice remains (proceeds)


def test_invariant_holds_across_all_leverage_types(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        z = _sec("ZZZ")
        _add(acct, InvTxnType.OPENING, datetime.date(2026, 1, 1), amount="10000")
        _add(acct, InvTxnType.SELL_SHORT, datetime.date(2026, 1, 5), security=z,
             qty="100", price="50", amount="5000")
        _add(acct, InvTxnType.DIV_PAID_SHORT, datetime.date(2026, 1, 8), security=z, amount="30")
        _add(acct, InvTxnType.BUY_TO_COVER, datetime.date(2026, 1, 10), security=z,
             qty="40", price="30", amount="1200")
        _add(acct, InvTxnType.BUY, datetime.date(2026, 1, 15), security=z,
             qty="300", price="50", amount="15000", fee="10")  # drives cash negative (margin)
        _add(acct, InvTxnType.MARGIN_INTEREST, datetime.date(2026, 1, 20), amount="40")
        _add(acct, InvTxnType.BUY_TO_COVER, datetime.date(2026, 1, 25), security=z,
             qty="60", price="35", amount="2100")
        assert _inv(acct)


# --- Capture views ----------------------------------------------------------------------------

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


def test_sell_short_and_cover_via_views(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        acct = _account(org=_brokerage())
        z = _sec("ZZZ")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        aid, zid = acct.pk, z.pk
    client.force_login(owner)

    resp = client.post(_url(tenant, f"accounts/{aid}/txns/new/"), {
        "txn_type": "sell_short", "date": "2026-02-02", "security": zid,
        "quantity": "100", "price": "50", "amount": "5000"})
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        lots = _open(acct, Security.objects.get(pk=zid))
        assert len(lots) == 1 and lots[0].remaining_quantity == D("-100")
        assert _inv(acct)

    resp = client.post(_url(tenant, f"accounts/{aid}/txns/new/"), {
        "txn_type": "buy_to_cover", "date": "2026-03-02", "security": zid,
        "quantity": "100", "price": "30", "amount": "3000"})
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        assert not _open(acct, Security.objects.get(pk=zid))
        assert InvestmentTransaction.objects.get(txn_type="buy_to_cover").realized_gain == D("2000")
        assert _inv(acct)


def test_margin_interest_via_view(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        acct = _account(org=_brokerage())
        _add(acct, InvTxnType.OPENING, JAN, amount="1000")
        aid = acct.pk
    client.force_login(owner)
    resp = client.post(_url(tenant, f"accounts/{aid}/txns/new/"), {
        "txn_type": "margin_interest", "date": "2026-02-02", "amount": "40"})
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        assert _bal("interest_expense") == D("40")
        assert _inv(acct)


def test_div_paid_short_via_view_requires_security(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        acct = _account(org=_brokerage())
        z = _sec("ZZZ")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        aid, zid = acct.pk, z.pk
    client.force_login(owner)

    # No security → rejected by the guard, nothing created.
    client.post(_url(tenant, f"accounts/{aid}/txns/new/"), {
        "txn_type": "div_paid_short", "date": "2026-03-02", "amount": "30"})
    with schema_context(tenant.schema_name):
        assert not InvestmentTransaction.objects.filter(txn_type="div_paid_short").exists()

    # With a security → posts to the substitute-dividend expense.
    resp = client.post(_url(tenant, f"accounts/{aid}/txns/new/"), {
        "txn_type": "div_paid_short", "date": "2026-03-02", "security": zid, "amount": "30"})
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        assert _bal("substitute_dividend_expense") == D("30")
        assert _inv(acct)
