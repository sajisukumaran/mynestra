"""Phase IP4 — corporate actions: stock-for-stock mergers, spin-offs, and ticker/symbol changes.
Covers the tax-lot engine (basis + holding-period carry, basis allocation), the cost-neutral
"nothing posts to the GL" rule, the `gl == cash + Σ open-lot cost` invariant, full-replay
correctness, the ticker-rename action, and the capture views. Mirrors the sibling suites' idioms."""

import datetime
from decimal import Decimal

from django_tenants.utils import schema_context

from apps.finance.models import Currency
from apps.finance.services import account_balance
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
    holdings,
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


# --- Merger ----------------------------------------------------------------------------------

def test_merger_swaps_security_carrying_basis(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        x = _sec("ACME")
        y = _sec("NEWCO")
        _add(acct, InvTxnType.OPENING, JAN, amount="1000")
        _add(acct, InvTxnType.BUY, FEB, security=x, qty="10", price="50", amount="500")
        gl_before = account_balance(acct.gl_account)

        merger = _add(acct, InvTxnType.MERGER, MAR, security=x, target_security=y,
                      split_ratio_new=D("1.5"), split_ratio_old=D("1"))

        assert not _open(acct, x)                       # original lots closed
        y_lots = _open(acct, y)
        assert len(y_lots) == 1
        assert y_lots[0].remaining_quantity == D("15")  # 10 × 1.5
        assert y_lots[0].cost_basis == D("500")         # basis carries over unchanged
        assert y_lots[0].acquired_date == FEB           # holding period preserved
        assert cost_basis(acct) == D("500")
        assert account_balance(acct.gl_account) == gl_before  # cash-neutral, nothing moved
        assert merger.journal_entry_id is None          # posts nothing
        assert _inv(acct)


def test_merger_posts_no_gl_entry_and_replays_on_buy_edit(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        x, y = _sec("ACME"), _sec("NEWCO")
        _add(acct, InvTxnType.OPENING, JAN, amount="2000")
        buy = _add(acct, InvTxnType.BUY, FEB, security=x, qty="10", price="50", amount="500")
        _add(acct, InvTxnType.MERGER, MAR, security=x, target_security=y,
             split_ratio_new=D("1"), split_ratio_old=D("1"))

        # Editing the earlier buy must replay through the merger into the Y lots.
        buy.amount = D("600")
        buy.save()
        apply_transaction(buy, is_new=False)
        y_lots = _open(acct, y)
        assert len(y_lots) == 1 and y_lots[0].cost_basis == D("600")

        # A later sale of the merged-into security works off the carried basis.
        sell = _add(acct, InvTxnType.SELL, APR, security=y, qty="10", price="80", amount="800")
        assert sell.realized_gain == D("200")           # 800 proceeds − 600 basis
        assert _inv(acct)


# --- Spin-off --------------------------------------------------------------------------------

def test_spinoff_allocates_basis_to_new_security(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        parent, spinco = _sec("PRNT", "Parent"), _sec("SPIN", "Spinco")
        _add(acct, InvTxnType.OPENING, JAN, amount="2000")
        _add(acct, InvTxnType.BUY, FEB, security=parent, qty="10", price="100", amount="1000")
        gl_before = account_balance(acct.gl_account)

        _add(acct, InvTxnType.SPINOFF, MAR, security=parent, target_security=spinco,
             split_ratio_new=D("0.5"), split_ratio_old=D("1"), basis_pct=D("20"))

        p_lots = _open(acct, parent)
        s_lots = _open(acct, spinco)
        assert len(p_lots) == 1 and p_lots[0].remaining_quantity == D("10")  # parent qty unchanged
        assert p_lots[0].cost_basis == D("800")          # 1000 − 20%
        assert len(s_lots) == 1
        assert s_lots[0].remaining_quantity == D("5")     # 10 × 0.5
        assert s_lots[0].cost_basis == D("200")           # 20% of 1000
        assert s_lots[0].acquired_date == FEB             # holding period tacks
        assert cost_basis(acct) == D("1000")              # total basis conserved
        assert account_balance(acct.gl_account) == gl_before
        assert _inv(acct)


def test_spinoff_conserves_basis_across_multiple_lots(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        parent, spinco = _sec("PRNT"), _sec("SPIN")
        _add(acct, InvTxnType.OPENING, JAN, amount="5000")
        _add(acct, InvTxnType.BUY, FEB, security=parent, qty="3", price="111", amount="333")
        _add(acct, InvTxnType.BUY, MAR, security=parent, qty="7", price="77", amount="539")
        _add(acct, InvTxnType.SPINOFF, APR, security=parent, target_security=spinco,
             split_ratio_new=D("1"), split_ratio_old=D("1"), basis_pct=D("33.3333"))
        # Per-lot X_after + Y == X_before exactly, so the total is preserved with no residual.
        assert cost_basis(acct) == D("872")               # 333 + 539
        assert _inv(acct)


# --- Ticker / symbol change ------------------------------------------------------------------

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


def test_ticker_change_renames_security_keeps_lots(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        org = _brokerage()
        acct = _account(org=org)
        fb = _sec("FB", "Facebook")
        _add(acct, InvTxnType.OPENING, JAN, amount="1000")
        _add(acct, InvTxnType.BUY, FEB, security=fb, qty="5", price="100", amount="500")
        sid = fb.pk
    client.force_login(owner)
    resp = client.post(_url(tenant, f"securities/{sid}/rename/"), {
        "new_symbol": "META", "new_name": "Meta Platforms", "effective_date": "2026-05-01"})
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        fb.refresh_from_db()
        assert fb.symbol == "META" and fb.name == "Meta Platforms"
        assert "Ticker changed FB → META effective 2026-05-01" in fb.notes
        # Same security row → lots + holdings still resolve under the new symbol.
        assert _open(acct, fb)
        held = {h.security.symbol: h.quantity for h in holdings(acct)}
        assert held.get("META") == D("5")
        assert fb.history.count() >= 2                     # create + rename


# --- Capture views ---------------------------------------------------------------------------

def test_merger_via_views_with_inline_target(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        org = _brokerage()
        acct = _account(org=org)
        x = _sec("ACME")
        _add(acct, InvTxnType.OPENING, JAN, amount="1000")
        _add(acct, InvTxnType.BUY, FEB, security=x, qty="10", price="50", amount="500")
        aid, xid = acct.pk, x.pk
    client.force_login(owner)
    resp = client.post(_url(tenant, f"accounts/{aid}/txns/new/"), {
        "txn_type": "merger", "date": "2026-03-02", "security": xid,
        "new_target_symbol": "NEWCO", "new_target_name": "NewCo Inc",
        "split_ratio_new": "2", "split_ratio_old": "1"})
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        y = Security.objects.get(symbol="NEWCO")         # created inline
        assert not _open(acct, x)
        y_lots = _open(acct, y)
        assert len(y_lots) == 1 and y_lots[0].remaining_quantity == D("20")
        assert y_lots[0].cost_basis == D("500")
        assert _inv(acct)


def test_spinoff_via_views_selecting_existing_target(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        org = _brokerage()
        acct = _account(org=org)
        parent, spinco = _sec("PRNT"), _sec("SPIN")
        _add(acct, InvTxnType.OPENING, JAN, amount="2000")
        _add(acct, InvTxnType.BUY, FEB, security=parent, qty="10", price="100", amount="1000")
        aid, pid, sid = acct.pk, parent.pk, spinco.pk
    client.force_login(owner)
    resp = client.post(_url(tenant, f"accounts/{aid}/txns/new/"), {
        "txn_type": "spinoff", "date": "2026-03-02", "security": pid,
        "target_security": sid, "split_ratio_new": "0.5", "split_ratio_old": "1",
        "basis_pct": "25"})
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        assert _open(acct, parent)[0].cost_basis == D("750")   # 1000 − 25%
        assert _open(acct, spinco)[0].cost_basis == D("250")
        assert cost_basis(acct) == D("1000")
        assert _inv(acct)


def test_spinoff_rejects_out_of_range_basis_pct(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        org = _brokerage()
        acct = _account(org=org)
        parent, spinco = _sec("PRNT"), _sec("SPIN")
        _add(acct, InvTxnType.OPENING, JAN, amount="2000")
        _add(acct, InvTxnType.BUY, FEB, security=parent, qty="10", price="100", amount="1000")
        aid, pid, sid = acct.pk, parent.pk, spinco.pk
    client.force_login(owner)
    resp = client.post(_url(tenant, f"accounts/{aid}/txns/new/"), {
        "txn_type": "spinoff", "date": "2026-03-02", "security": pid,
        "target_security": sid, "split_ratio_new": "1", "split_ratio_old": "1",
        "basis_pct": "150"})  # > 100 → rejected by the guard, no txn created
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        assert not InvestmentTransaction.objects.filter(txn_type="spinoff").exists()


def test_spinoff_with_blank_basis_keeps_all_cost_on_parent(make_tenant, make_user, client):
    """Basis % is optional: blank keeps all cost basis on the parent (X) and creates the spun-off
    shares (Y) at $0 basis. The parent's share count is unchanged — a spin-off does not dissolve it
    — and the action stays cash-neutral (nothing posts to the ledger, the invariant holds)."""
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        org = _brokerage()
        acct = _account(org=org)
        parent, spinco = _sec("VZ"), _sec("FTR")
        _add(acct, InvTxnType.OPENING, JAN, amount="2000")
        _add(acct, InvTxnType.BUY, FEB, security=parent, qty="70", price="20", amount="1400")
        aid, pid, sid = acct.pk, parent.pk, spinco.pk
    client.force_login(owner)
    resp = client.post(_url(tenant, f"accounts/{aid}/txns/new/"), {
        "txn_type": "spinoff", "date": "2026-03-02", "security": pid,
        "target_security": sid, "split_ratio_new": "16", "split_ratio_old": "70",
        "basis_pct": ""})  # blank → keep all cost on VZ
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        vz, ftr = _open(acct, parent), _open(acct, spinco)
        assert vz[0].remaining_quantity == D("70")   # VZ shares untouched (not dissolved)
        assert vz[0].cost_basis == D("1400")          # VZ keeps 100% of its cost
        assert ftr[0].remaining_quantity == D("16")   # received 16 FTR (70 × 16/70)
        assert ftr[0].cost_basis == D("0")            # $0 basis until allocated
        assert cost_basis(acct) == D("1400")          # basis conserved
        assert _inv(acct)                             # invariant holds


def test_spinoff_with_cash_in_lieu_lands_on_whole_shares(make_tenant, make_user, client):
    """One-step spin-off with cash-in-lieu (the E*TRADE VZ -> FTR case): entering the FULL
    fractional entitlement in the ratio plus the cash sells the fraction, so the account lands on
    WHOLE Y shares + the cash in a single transaction, X is untouched, and the gain posts to
    Realized Capital Gain/Loss. Basis blank keeps all cost on X, so the $0-basis fraction sells for
    the full cash and the invariant holds exactly."""
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        org = _brokerage()
        acct = _account(org=org)
        vz, ftr = _sec("VZ"), _sec("FTR")
        _add(acct, InvTxnType.OPENING, JAN, amount="2000")
        _add(acct, InvTxnType.BUY, FEB, security=vz, qty="70", price="20", amount="1400")
        aid, vid, fid = acct.pk, vz.pk, ftr.pk
    client.force_login(owner)
    resp = client.post(_url(tenant, f"accounts/{aid}/txns/new/"), {
        "txn_type": "spinoff", "date": "2026-03-02", "security": vid,
        "target_security": fid, "split_ratio_new": "16.80278", "split_ratio_old": "70",
        "basis_pct": "", "cash_in_lieu": "5.74"})  # 16.80278 entitlement, cash for the 0.80278 frac
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        assert _open(acct, vz)[0].remaining_quantity == D("70")    # VZ untouched
        assert _open(acct, ftr)[0].remaining_quantity == D("16")   # 16.80278 less 0.80278 frac
        spin = InvestmentTransaction.objects.get(account_id=aid, txn_type="spinoff")
        assert spin.realized_gain == D("5.74")     # $0-basis fraction sold for the cash
        assert cash_balance(acct) == D("605.74")   # 2000 − 1400 + 5.74
        assert _inv(acct)                          # exact — no sub-cent basis on the fraction


def test_spinoff_edit_adds_cash_in_lieu(make_tenant, make_user, client):
    """Editing an existing plain spin-off to add cash-in-lieu sells the fraction and brings the cash
    in (the edit / repost path, not just create) — cash total moves by the cash-in-lieu."""
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        org = _brokerage()
        acct = _account(org=org)
        vz, ftr = _sec("VZ"), _sec("FTR")
        _add(acct, InvTxnType.OPENING, JAN, amount="2000")
        _add(acct, InvTxnType.BUY, FEB, security=vz, qty="70", price="20", amount="1400")
        aid, vid, fid = acct.pk, vz.pk, ftr.pk
    client.force_login(owner)
    # Plain spin-off first (full entitlement, no cash) → 16.80278 FTR, cash-neutral.
    client.post(_url(tenant, f"accounts/{aid}/txns/new/"), {
        "txn_type": "spinoff", "date": "2026-03-02", "security": vid,
        "target_security": fid, "split_ratio_new": "16.80278", "split_ratio_old": "70",
        "basis_pct": ""})
    with schema_context(tenant.schema_name):
        spin_id = InvestmentTransaction.objects.get(account_id=aid, txn_type="spinoff").pk
        assert cash_balance(acct) == D("600")  # cash-neutral so far
    # Now edit it to add the cash in lieu of the fraction.
    resp = client.post(_url(tenant, f"accounts/{aid}/txns/{spin_id}/edit/"), {
        "txn_type": "spinoff", "date": "2026-03-02", "security": vid,
        "target_security": fid, "split_ratio_new": "16.80278", "split_ratio_old": "70",
        "basis_pct": "", "cash_in_lieu": "5.74"})
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        spin = InvestmentTransaction.objects.get(pk=spin_id)
        assert spin.amount == D("5.74")                            # cash-in-lieu persisted
        assert _open(acct, ftr)[0].remaining_quantity == D("16")   # fraction sold on repost
        assert cash_balance(acct) == D("605.74")                   # cash total moved by +5.74
        assert _inv(acct)


# --- Cash in lieu of a fractional share (spin-off tail) --------------------------------------

def test_spinoff_then_cash_in_lieu_of_fractional(make_tenant):
    """Real-world VZ -> FTR (2010): a spin-off yields a fractional entitlement, then cash-in-lieu
    sells that fraction — whole shares held, the fraction sold for cash, invariant intact."""
    with schema_context(make_tenant().schema_name):
        acct = _account()
        vz, ftr = _sec("VZ", "Verizon"), _sec("FTR", "Frontier")
        _add(acct, InvTxnType.OPENING, JAN, amount="3000")
        _add(acct, InvTxnType.BUY, FEB, security=vz, qty="70", price="30", amount="2100")
        # Spin-off: 16.80278 FTR on 70 VZ; 10% of the VZ basis allocated to FTR.
        _add(acct, InvTxnType.SPINOFF, MAR, security=vz, target_security=ftr,
             split_ratio_new=D("16.80278"), split_ratio_old=D("70"), basis_pct=D("10"))
        assert _open(acct, ftr)[0].remaining_quantity == D("16.802780")
        cash_before = cash_balance(acct)

        # Cash in lieu of the 0.80278 fractional share; $5.74 received.
        cil = _add(acct, InvTxnType.CASH_IN_LIEU, APR, security=ftr, qty="0.80278", amount="5.74")

        ftr_hold = next(h for h in holdings(acct) if h.security.id == ftr.id)
        acct.refresh_from_db()
        assert ftr_hold.quantity == D("16")                    # whole shares remain
        assert cash_balance(acct) == cash_before + D("5.74")   # cash-in-lieu proceeds arrived
        assert cil.realized_gain != D("0")                     # gain/loss realized on the fraction
        # A fractional disposition's gain has sub-cent precision; the GL posts money in whole cents
        # (finance quantizes to the currency), so gl matches cash + cost to within a cent — the
        # exact-equality invariant holds only for whole-cent (whole-share) dispositions.
        drift = account_balance(acct.gl_account) - (cash_balance(acct) + cost_basis(acct))
        assert abs(drift) <= D("0.01")


def test_cash_in_lieu_behaves_like_a_sell(make_tenant):
    """A cash-in-lieu is accounting-identical to a sell: consumes lots, realizes gain, cash in."""
    with schema_context(make_tenant().schema_name):
        acct = _account()
        s = _sec("ACME")
        _add(acct, InvTxnType.OPENING, JAN, amount="1000")
        _add(acct, InvTxnType.BUY, FEB, security=s, qty="10", price="50", amount="500")
        cil = _add(acct, InvTxnType.CASH_IN_LIEU, MAR, security=s, qty="2", amount="140")
        assert cil.realized_gain == D("40")            # proceeds 140 - cost 100 (2 @ 50)
        assert next(h for h in holdings(acct) if h.security.id == s.id).quantity == D("8")
        assert _inv(acct)
