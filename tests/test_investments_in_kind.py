"""Phase IP2 — in-kind securities transfers (internal + external), worthless write-offs, and cash
buyouts / mergers. Covers the tax-lot engine, the cost-in-the-ledger posting, the paired-leg sync
that keeps 1150 clearing at zero, the `gl == cash + Σ open-lot cost` invariant, models, and the
capture views. Mirrors the `_setup()`/`_add()`/`apply_transaction` idioms of the sibling suites."""

import datetime
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django_tenants.utils import schema_context

from apps.finance.models import Currency, JournalLine
from apps.finance.services import account_balance, resolve_account
from apps.investments.models import (
    InvestmentAccount,
    InvestmentTransaction,
    InvTxnType,
    Lot,
    Security,
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


# --- Service-level helpers (inside schema_context) -------------------------------------------

def _account(nickname="Taxable", registration="taxable_individual", org=None):
    acct = InvestmentAccount.objects.create(
        institution=org or Organization.objects.create(name="Broker"),
        nickname=nickname, registration=registration,
        currency=Currency.objects.get(code="USD"),
    )
    ensure_gl_account(acct)
    return acct


def _setup():
    acct = _account()
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


def _two():
    """Two GL-backed accounts sharing one broker; `a` funded (opening 1000) and holding 10 ACME at a
    500 cost basis acquired FEB; `b` empty."""
    org = Organization.objects.create(name="Broker")
    a = _account("Taxable A", org=org)
    b = _account("Roth B", registration="roth_ira", org=org)
    sec = Security.objects.create(
        symbol="ACME", name="Acme", currency=Currency.objects.get(code="USD"))
    _add(a, InvTxnType.OPENING, JAN, amount="1000")
    _add(a, InvTxnType.BUY, FEB, security=sec, qty="10", price="50", amount="500")
    return a, b, sec


def _inv(acct) -> bool:
    """The core invariant: the postable GL node equals settlement cash + Σ open-lot cost basis."""
    acct.refresh_from_db()
    return account_balance(acct.gl_account) == cash_balance(acct) + cost_basis(acct)


# --- Engine + posting ------------------------------------------------------------------------

def test_in_kind_out_external_consumes_at_cost_no_gain(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        _add(acct, InvTxnType.OPENING, JAN, amount="1000")
        _add(acct, InvTxnType.BUY, FEB, security=sec, qty="10", price="50", amount="500")
        out = _add(acct, InvTxnType.IN_KIND_OUT, MAR, security=sec, qty="4")  # external (no dest)
        assert out.realized_gain == D("0")
        # Snapshot preserves the acquired date + cost of the lots actually consumed (4 of 10 → 200).
        assert out.lot_carry == [
            {"acquired_date": FEB.isoformat(), "quantity": "4.000000", "cost": "200.0000"}
        ]
        assert cost_basis(acct) == D("300")  # 6 shares remain at a 300 basis
        eq = resolve_account("opening_balance_equity")
        # Value leaves the tracked books at cost.
        assert JournalLine.objects.filter(account=eq, debit=D("200")).exists()
        assert _inv(acct)


def test_external_in_kind_in_recreates_lots_preserving_basis(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        carry = [
            {"acquired_date": "2020-06-01", "quantity": "10", "cost": "1000"},
            {"acquired_date": "2021-06-01", "quantity": "5", "cost": "800"},
        ]
        _add(acct, InvTxnType.IN_KIND_IN, JAN, security=sec, lot_carry=carry)
        lots = list(
            Lot.objects.filter(account=acct, security=sec, open=True).order_by("acquired_date")
        )
        assert len(lots) == 2
        assert lots[0].acquired_date == datetime.date(2020, 6, 1)
        assert lots[0].cost_basis == D("1000")
        assert lots[1].acquired_date == datetime.date(2021, 6, 1)
        assert lots[1].cost_basis == D("800")
        assert cost_basis(acct) == D("1800")
        eq = resolve_account("opening_balance_equity")
        # Value enters the tracked books at cost.
        assert JournalLine.objects.filter(account=eq, credit=D("1800")).exists()
        assert _inv(acct)


def test_worthless_writes_off_position_as_capital_loss(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        _add(acct, InvTxnType.OPENING, JAN, amount="1000")
        _add(acct, InvTxnType.BUY, FEB, security=sec, qty="10", price="50", amount="500")
        wo = _add(acct, InvTxnType.WORTHLESS, MAR, security=sec)
        assert wo.realized_gain == D("-500")
        assert cost_basis(acct) == D("0")
        assert not Lot.objects.filter(account=acct, security=sec, open=True).exists()
        assert account_balance(resolve_account("realized_capital_gain")) == D("-500")  # a loss
        assert cash_balance(acct) == D("500")  # unaffected by the write-off
        assert _inv(acct)


def test_cash_merger_disposes_whole_position_for_cash(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        _add(acct, InvTxnType.OPENING, JAN, amount="1000")
        _add(acct, InvTxnType.BUY, FEB, security=sec, qty="10", price="50", amount="500")
        cm = _add(acct, InvTxnType.CASH_MERGER, MAR, security=sec, amount="700")
        assert cm.realized_gain == D("200")  # 700 buyout − 500 basis
        assert cost_basis(acct) == D("0")
        assert cash_balance(acct) == D("1200")  # 500 + 700 buyout cash
        assert account_balance(resolve_account("realized_capital_gain")) == D("200")
        assert _inv(acct)


def test_internal_transfer_creates_mirror_and_nets_clearing(make_tenant):
    with schema_context(make_tenant().schema_name):
        a, b, sec = _two()
        out = _add(a, InvTxnType.IN_KIND_OUT, MAR, security=sec, qty="4",
                   counter_investment_account=b)
        in_leg = InvestmentTransaction.objects.get(account=b, txn_type=InvTxnType.IN_KIND_IN)
        assert in_leg.counter_investment_account_id == a.id
        assert in_leg.paired_txn_id == out.id
        out.refresh_from_db()
        assert out.paired_txn_id == in_leg.id
        # b now holds the 4 shares at their original basis and acquired date.
        blot = Lot.objects.get(account=b, security=sec, open=True)
        assert blot.remaining_quantity == D("4")
        assert blot.cost_basis == D("200")
        assert blot.acquired_date == FEB
        assert account_balance(resolve_account("transfer_clearing")) == D("0")  # 1150 nets out
        assert _inv(a) and _inv(b)


def test_external_in_kind_out_posts_against_opening_equity(make_tenant):
    with schema_context(make_tenant().schema_name):
        a, _b, sec = _two()
        _add(a, InvTxnType.IN_KIND_OUT, MAR, security=sec, qty="10")  # external — whole position
        assert cost_basis(a) == D("0")
        # No mirror leg, and the clearing account is untouched (external transfers use equity).
        assert not InvestmentTransaction.objects.filter(txn_type=InvTxnType.IN_KIND_IN).exists()
        assert _inv(a)


def test_editing_out_leg_resyncs_mirror(make_tenant):
    with schema_context(make_tenant().schema_name):
        a, b, sec = _two()
        out = _add(a, InvTxnType.IN_KIND_OUT, MAR, security=sec, qty="4",
                   counter_investment_account=b)
        out.quantity = D("8")
        out.save()
        apply_transaction(out, is_new=False)
        blot = Lot.objects.get(account=b, security=sec, open=True)
        assert blot.remaining_quantity == D("8")
        assert blot.cost_basis == D("400")
        assert cost_basis(a) == D("100")  # 2 shares remain in the source
        assert account_balance(resolve_account("transfer_clearing")) == D("0")
        assert _inv(a) and _inv(b)


def test_deleting_out_leg_removes_mirror(make_tenant):
    with schema_context(make_tenant().schema_name):
        a, b, sec = _two()
        out = _add(a, InvTxnType.IN_KIND_OUT, MAR, security=sec, qty="4",
                   counter_investment_account=b)
        remove_transaction(out)
        assert not InvestmentTransaction.objects.filter(
            account=b, txn_type=InvTxnType.IN_KIND_IN).exists()
        assert not Lot.objects.filter(account=b, security=sec, open=True).exists()
        assert cost_basis(a) == D("500")  # source position restored in full
        assert account_balance(resolve_account("transfer_clearing")) == D("0")
        assert _inv(a) and _inv(b)


def test_editing_earlier_buy_replays_into_worthless_loss(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        _add(acct, InvTxnType.OPENING, JAN, amount="2000")
        buy = _add(acct, InvTxnType.BUY, FEB, security=sec, qty="10", price="50", amount="500")
        wo = _add(acct, InvTxnType.WORTHLESS, MAR, security=sec)
        assert wo.realized_gain == D("-500")
        buy.amount = D("600")  # raise basis — the later write-off loss must grow on replay
        buy.save()
        apply_transaction(buy, is_new=False)
        wo.refresh_from_db()
        assert wo.realized_gain == D("-600")
        assert account_balance(resolve_account("realized_capital_gain")) == D("-600")
        assert _inv(acct)


def test_invariant_holds_across_all_ip2_types(make_tenant):
    with schema_context(make_tenant().schema_name):
        a, b, sec = _two()
        usd = Currency.objects.get(code="USD")
        dead = Security.objects.create(symbol="DEAD", name="Deadco", currency=usd)
        buyo = Security.objects.create(symbol="BUYO", name="Buyout", currency=usd)
        _add(a, InvTxnType.BUY, FEB, security=dead, qty="5", price="20", amount="100")
        _add(a, InvTxnType.BUY, FEB, security=buyo, qty="2", price="150", amount="300")
        _add(a, InvTxnType.IN_KIND_OUT, MAR, security=sec, qty="4", counter_investment_account=b)
        _add(a, InvTxnType.IN_KIND_OUT, MAR, security=sec, qty="1")            # external gift
        _add(a, InvTxnType.WORTHLESS, MAR, security=dead)
        _add(a, InvTxnType.CASH_MERGER, MAR, security=buyo, amount="400")
        assert _inv(a) and _inv(b)


# --- Models ----------------------------------------------------------------------------------

def test_signed_cash_for_ip2_types(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, _sec = _setup()

        def sc(ttype, **kw):
            return InvestmentTransaction(account=acct, txn_type=ttype, **kw).signed_cash

        assert sc(InvTxnType.CASH_MERGER, amount=D("700")) == D("700")  # buyout cash in
        assert sc(InvTxnType.IN_KIND_OUT, quantity=D("5")) == D("0")    # cash-neutral
        assert sc(InvTxnType.IN_KIND_IN) == D("0")
        assert sc(InvTxnType.WORTHLESS) == D("0")


def test_no_self_inkind_constraint(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        with pytest.raises(IntegrityError), transaction.atomic():
            InvestmentTransaction.objects.create(
                account=acct, txn_type=InvTxnType.IN_KIND_OUT, date=JAN,
                security=sec, counter_investment_account=acct)


def test_is_managed_in_leg_property(make_tenant):
    with schema_context(make_tenant().schema_name):
        a, b, sec = _two()
        out = _add(a, InvTxnType.IN_KIND_OUT, MAR, security=sec, qty="4",
                   counter_investment_account=b)
        in_leg = InvestmentTransaction.objects.get(account=b, txn_type=InvTxnType.IN_KIND_IN)
        assert in_leg.is_managed_in_leg is True   # internal mirror (carries the source account)
        assert out.is_managed_in_leg is False      # the OUT leg is not a mirror
        ext = _add(a, InvTxnType.IN_KIND_IN, MAR, security=sec,
                   lot_carry=[{"acquired_date": "2020-01-01", "quantity": "1", "cost": "10"}])
        assert ext.is_managed_in_leg is False       # external IN is user-entered, not managed


# --- Views -----------------------------------------------------------------------------------

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


def _view_two():
    """Two GL-backed accounts; `a` holds 10 ACME at a 500 basis. Call inside a schema_context."""
    org = _brokerage()
    a = _account("Taxable A", org=org)
    b = _account("Roth B", registration="roth_ira", org=org)
    sec = Security.objects.create(
        symbol="ACME", name="Acme", currency=Currency.objects.get(code="USD"))
    _add(a, InvTxnType.OPENING, JAN, amount="1000")
    _add(a, InvTxnType.BUY, FEB, security=sec, qty="10", price="50", amount="500")
    return a, b, sec


def test_internal_in_kind_out_via_views_creates_mirror(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        a, b, sec = _view_two()
        aid, bid, sid = a.pk, b.pk, sec.pk
    client.force_login(owner)
    resp = client.post(_url(tenant, f"accounts/{aid}/txns/new/"), {
        "txn_type": "in_kind_out", "date": "2026-03-02", "security": sid,
        "quantity": "4", "counter_investment_account": bid})
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        in_leg = InvestmentTransaction.objects.get(account_id=bid, txn_type="in_kind_in")
        assert in_leg.counter_investment_account_id == aid
        assert Lot.objects.filter(account_id=bid, security_id=sid, open=True).exists()
        assert account_balance(resolve_account("transfer_clearing")) == D("0")


def test_external_multi_lot_in_kind_in_via_views(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        a, _b, _sec = _view_two()
        # A fresh security so the account holds only the two incoming (gifted/inherited) lots.
        gift = Security.objects.create(
            symbol="GIFT", name="Gifted", currency=Currency.objects.get(code="USD"))
        aid, gid = a.pk, gift.pk
    client.force_login(owner)
    resp = client.post(_url(tenant, f"accounts/{aid}/txns/new/"), {
        "txn_type": "in_kind_in", "date": "2026-03-02", "security": gid,
        "lot_acquired": ["2020-06-01", "2021-06-01"],
        "lot_qty": ["10", "5"],
        "lot_cost": ["1000", "800"]})
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        lots = list(
            Lot.objects.filter(account_id=aid, security_id=gid, open=True).order_by("acquired_date")
        )
        assert len(lots) == 2
        assert lots[0].cost_basis == D("1000")
        assert lots[1].cost_basis == D("800")


def test_worthless_and_cash_merger_via_views(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        a, _b, sec = _view_two()
        dead = Security.objects.create(
            symbol="DEAD", name="Deadco", currency=Currency.objects.get(code="USD"))
        aid, sid, did = a.pk, sec.pk, dead.pk
    client.force_login(owner)
    client.post(_url(tenant, f"accounts/{aid}/txns/new/"), {
        "txn_type": "buy", "date": "2026-02-10", "security": did,
        "quantity": "5", "price": "10", "amount": "50", "fee": "0"})
    client.post(_url(tenant, f"accounts/{aid}/txns/new/"), {
        "txn_type": "worthless", "date": "2026-03-02", "security": did})
    client.post(_url(tenant, f"accounts/{aid}/txns/new/"), {
        "txn_type": "cash_merger", "date": "2026-03-05", "security": sid, "amount": "700"})
    with schema_context(tenant.schema_name):
        assert InvestmentTransaction.objects.get(
            account_id=aid, txn_type="worthless").realized_gain == D("-50")
        assert InvestmentTransaction.objects.get(
            account_id=aid, txn_type="cash_merger").realized_gain == D("200")
        assert not Lot.objects.filter(account_id=aid, open=True).exists()


def test_managed_in_leg_edit_and_delete_rejected(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        a, b, sec = _view_two()
        aid, bid, sid = a.pk, b.pk, sec.pk
    client.force_login(owner)
    client.post(_url(tenant, f"accounts/{aid}/txns/new/"), {
        "txn_type": "in_kind_out", "date": "2026-03-02", "security": sid,
        "quantity": "4", "counter_investment_account": bid})
    with schema_context(tenant.schema_name):
        leg_id = InvestmentTransaction.objects.get(account_id=bid, txn_type="in_kind_in").pk
    # A direct delete of the managed mirror leg must be a no-op (it is maintained via its OUT leg).
    client.post(_url(tenant, f"accounts/{bid}/txns/{leg_id}/delete/"), {})
    with schema_context(tenant.schema_name):
        assert InvestmentTransaction.objects.filter(pk=leg_id).exists()
        assert account_balance(resolve_account("transfer_clearing")) == D("0")
