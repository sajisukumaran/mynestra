"""Investments service layer — the bridge from investment transactions to the general ledger, plus
the tax-lot engine.

Two coupled responsibilities:

1. **Tax-lot engine (cost basis).** Buys/opening-holdings/reinvested-dividends create `Lot`s; sells
   consume them (FIFO or specific) into `LotConsumption`s and compute a realized gain; splits scale
   lots; return-of-capital reduces basis. Because lot state is order-dependent, it is never mutated
   in place per edit — instead `rebuild_account_lots` **replays the whole register** in date order.
   That makes every edit/delete trivially correct (just replay) at household scale.

2. **GL posting (cost in the ledger).** Each transaction posts a balanced journal entry through
   `apps.finance.services` (never a hand-written row). The account's one postable ledger node holds
   it **at cost** — so buys/splits and cash-neutral moves post **nothing**; only money in/out,
   income (dividends/interest/cap-gain distributions) and realized gains hit it. Posted entries are
   immutable, so an edit is reverse-and-repost (bumping `posting_version`).

Invariant (asserted in tests): `account_balance(gl) == cash_balance + Σ open-lot cost_basis`.

The two are orchestrated by `apply_transaction` / `remove_transaction`: rebuild lots first (so any
sell's realized gain is current), then post the changed transaction and re-post any *other* sell
whose realized gain shifted as a result.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

from apps.finance.models import ZERO, Account, AccountType, JournalEntry, Side
from apps.finance.services import (
    LineInput,
    post_entry,
    resolve_account,
    resolve_posting_account,
    reverse_entry,
)
from apps.investments.exceptions import InsufficientShares
from apps.investments.models import (
    GROUP_HEADER_KEY,
    InvestmentAccount,
    InvestmentTransaction,
    InvTxnType,
    Lot,
    LotConsumption,
    Security,
)

# --- Fixed contras / remappable activities ---------------------------------------------------

OPENING_EQUITY = "opening_balance_equity"      # 3100
TRANSFER_CLEARING = "transfer_clearing"        # 1150
DIVIDEND_INCOME = "dividend_income"            # 4310
REALIZED_GAIN = "realized_capital_gain"        # 4320 (gains credit, losses debit)
CAPGAIN_DIST = "capital_gains_distribution"    # 4330
INVEST_INTEREST = "investment_interest"        # 4340
INVEST_FEES = "investment_fees"                # 5870

# Category legs the Expert-mode Accounting Setup tab can remap, per investment account. Structural
# legs (opening equity, transfer clearing, realized-gain) are never remappable.
POSTING_ACTIVITIES = [
    {"key": "dividend_income", "label": "Dividends", "kind": "income", "default": DIVIDEND_INCOME},
    {"key": "investment_interest", "label": "Interest", "kind": "income",
     "default": INVEST_INTEREST},
    {"key": "capital_gains_distribution", "label": "Capital-gain distributions", "kind": "income",
     "default": CAPGAIN_DIST},
    {"key": "fee_expense", "label": "Fees", "kind": "expense", "default": INVEST_FEES},
]

_CENTS = Decimal("0.0001")
_SHARE = Decimal("0.000001")


def _q_amount(x) -> Decimal:
    return Decimal(x).quantize(_CENTS)


def _q_qty(x) -> Decimal:
    return Decimal(x).quantize(_SHARE)


def _carry_total(carry) -> Decimal:
    """Total cost basis of an in-kind `lot_carry` snapshot."""
    return _q_amount(sum((Decimal(str(e["cost"])) for e in (carry or [])), ZERO))


# --- GL account provisioning -----------------------------------------------------------------

def _gl_name(account: InvestmentAccount) -> str:
    masked = account.masked_number
    return f"{account.nickname} {masked}".strip() if masked else account.nickname


def _group_header(account: InvestmentAccount) -> Account:
    return resolve_account(GROUP_HEADER_KEY[account.group])


def _next_child_code(parent: Account) -> str:
    """The next free `<parent.code>.NN` code under a group header (e.g. 1210.01, 1220.03)."""
    prefix = f"{parent.code}."
    highest = 0
    for code in Account.objects.filter(parent=parent).values_list("code", flat=True):
        if code.startswith(prefix):
            suffix = code[len(prefix):]
            if suffix.isdigit():
                highest = max(highest, int(suffix))
    return f"{parent.code}.{highest + 1:02d}"


def ensure_gl_account(account: InvestmentAccount, *, parent=None, existing=None) -> Account:
    """Create (or refresh) the postable ledger account carrying this investment account at cost.

    Standard mode auto-creates a child under the group header matching the account's registration
    (1210 taxable / 1220 retirement / 1230 HSA). Expert mode may pass a different `parent` header or
    an `existing` postable account to adopt."""
    if account.gl_account_id:
        gl = account.gl_account
        changed = []
        name = _gl_name(account)
        if gl.name != name:
            gl.name = name
            changed.append("name")
        if gl.currency_id != account.currency_id and not gl.lines.exists():
            gl.currency = account.currency
            changed.append("currency")
        if changed:
            gl.save(update_fields=[*changed, "updated_at"])
        return gl

    if existing is not None:
        account.gl_account = existing
        account.save(update_fields=["gl_account"])
        return existing

    parent = parent or _group_header(account)
    gl = Account.objects.create(
        code=_next_child_code(parent),
        name=_gl_name(account),
        type=AccountType.ASSET,
        normal_side=Side.DEBIT,
        currency=account.currency,
        parent=parent,
        is_postable=True,
        is_system=False,
    )
    account.gl_account = gl
    account.save(update_fields=["gl_account"])
    return gl


# --- Tax-lot engine --------------------------------------------------------------------------

def _open_lots(account, security):
    return list(
        Lot.objects.filter(account=account, security=security, open=True).order_by(
            "acquired_date", "id"
        )
    )


def _plan_draws(txn) -> list[tuple[Lot, Decimal]]:
    """Which lots (and how much of each) a SELL draws from — FIFO by default, else the specific
    lots the user chose (keyed by source buy txn, which survives a replay)."""
    account, security = txn.account, txn.security
    qty_needed = _q_qty(txn.quantity)
    open_lots = _open_lots(account, security)

    draws: list[tuple[Lot, Decimal]] = []
    if txn.cost_basis_method == "specific" and txn.lot_selection:
        by_src = {lot.source_txn_id: lot for lot in open_lots}
        for sel in txn.lot_selection:
            lot = by_src.get(sel.get("buy_txn"))
            take = _q_qty(sel.get("qty", 0))
            if lot is None or take <= ZERO or take > lot.remaining_quantity:
                raise InsufficientShares(
                    f"Lot for {security} unavailable or insufficient for the selected sale."
                )
            draws.append((lot, take))
        if _q_qty(sum(q for _, q in draws)) != qty_needed:
            raise InsufficientShares("Selected lots do not sum to the sale quantity.")
        return draws

    remaining = qty_needed
    for lot in open_lots:
        if remaining <= ZERO:
            break
        take = min(lot.remaining_quantity, remaining)
        if take > ZERO:
            draws.append((lot, _q_qty(take)))
            remaining = _q_qty(remaining - take)
    if remaining > ZERO:
        raise InsufficientShares(
            f"Not enough shares of {security} to sell {qty_needed} (short by {remaining})."
        )
    return draws


def _consume_draws(txn, draws, net_proceeds) -> Decimal:
    """Consume the given lot draws at cost, allocating `net_proceeds` across them (recording a
    LotConsumption per draw), and return the realized gain (proceeds − cost). Shared by SELL,
    cash-merger (whole position for the buyout cash) and worthless (whole position for zero)."""
    net_proceeds = _q_amount(net_proceeds)
    total_qty = _q_qty(sum((q for _, q in draws), ZERO))
    total_cost = ZERO
    allocated = ZERO
    n = len(draws)
    for i, (lot, take) in enumerate(draws):
        if lot.remaining_quantity and lot.remaining_quantity != ZERO:
            cost = _q_amount(lot.cost_basis * (take / lot.remaining_quantity))
        else:
            cost = ZERO
        if i == n - 1:
            proceeds = _q_amount(net_proceeds - allocated)
        else:
            proceeds = _q_amount(net_proceeds * (take / total_qty)) if total_qty else ZERO
        allocated = _q_amount(allocated + proceeds)

        lot.remaining_quantity = _q_qty(lot.remaining_quantity - take)
        lot.cost_basis = _q_amount(lot.cost_basis - cost)
        if lot.remaining_quantity <= ZERO:
            lot.remaining_quantity = ZERO
            lot.cost_basis = ZERO
            lot.open = False
        lot.save(update_fields=["remaining_quantity", "cost_basis", "open", "updated_at"])
        LotConsumption.objects.create(
            sale_txn=txn, lot=lot, quantity=take, cost=cost, proceeds=proceeds
        )
        total_cost = _q_amount(total_cost + cost)
    return _q_amount(net_proceeds - total_cost)


def _consume_lots(txn) -> Decimal:
    """Draw the sale's quantity from lots (FIFO or specific); return the realized gain."""
    return _consume_draws(txn, _plan_draws(txn), txn.net_proceeds)


def _all_open_draws(txn) -> list[tuple[Lot, Decimal]]:
    """Every open lot of the transaction's security, drawn in full — for whole-position events
    (worthless write-off, cash buyout/merger)."""
    return [(lot, lot.remaining_quantity) for lot in _open_lots(txn.account, txn.security)]


def _apply_worthless(txn) -> Decimal:
    """Write the entire position off: dispose every open lot at cost for zero proceeds, realizing a
    capital loss equal to the remaining basis. Cash-neutral."""
    return _consume_draws(txn, _all_open_draws(txn), ZERO)


def _apply_cash_merger(txn) -> Decimal:
    """Cash buyout of the whole position: dispose every open lot for the buyout cash, realizing the
    gain/loss (a full sell whose proceeds are the cash received)."""
    return _consume_draws(txn, _all_open_draws(txn), _q_amount(txn.amount))


def _apply_split(txn) -> None:
    if not (txn.split_ratio_new and txn.split_ratio_old):
        return
    ratio = txn.split_ratio_new / txn.split_ratio_old
    for lot in _open_lots(txn.account, txn.security):
        lot.remaining_quantity = _q_qty(lot.remaining_quantity * ratio)
        lot.original_quantity = _q_qty(lot.original_quantity * ratio)
        lot.save(update_fields=["remaining_quantity", "original_quantity", "updated_at"])


def _apply_return_of_capital(txn) -> Decimal:
    """Reduce open-lot basis by the distribution; any excess over total basis is a realized gain."""
    lots = _open_lots(txn.account, txn.security)
    total_basis = _q_amount(sum(lot.cost_basis for lot in lots))
    amount = _q_amount(txn.amount)
    if amount <= total_basis and total_basis > ZERO:
        reduced = ZERO
        n = len(lots)
        for i, lot in enumerate(lots):
            if i == n - 1:
                cut = _q_amount(amount - reduced)
            else:
                cut = _q_amount(amount * (lot.cost_basis / total_basis))
            lot.cost_basis = _q_amount(lot.cost_basis - cut)
            reduced = _q_amount(reduced + cut)
            lot.save(update_fields=["cost_basis", "updated_at"])
        return ZERO
    # Basis exhausted — zero every lot and recognize the excess as a realized gain.
    for lot in lots:
        lot.cost_basis = ZERO
        lot.save(update_fields=["cost_basis", "updated_at"])
    return _q_amount(amount - total_basis)


def _create_lot(txn, cost: Decimal) -> None:
    qty = _q_qty(txn.quantity)
    Lot.objects.create(
        account=txn.account,
        security=txn.security,
        acquired_date=txn.date,
        original_quantity=qty,
        remaining_quantity=qty,
        original_cost=cost,
        cost_basis=cost,
        open=qty > ZERO,
        source_txn=txn,
    )


def _apply_in_kind_out(txn) -> Decimal:
    """Consume lots at cost (FIFO or specific), realizing NO gain, and materialize the consumed
    lots onto `txn.lot_carry` (persisted by `rebuild_account_lots`) so the paired IN leg can
    recreate them with their original acquisition date + cost basis."""
    draws = _plan_draws(txn)
    carry = []
    for lot, take in draws:
        if lot.remaining_quantity and lot.remaining_quantity != ZERO:
            cost = _q_amount(lot.cost_basis * (take / lot.remaining_quantity))
        else:
            cost = ZERO
        acquired = lot.acquired_date
        lot.remaining_quantity = _q_qty(lot.remaining_quantity - take)
        lot.cost_basis = _q_amount(lot.cost_basis - cost)
        if lot.remaining_quantity <= ZERO:
            lot.remaining_quantity = ZERO
            lot.cost_basis = ZERO
            lot.open = False
        lot.save(update_fields=["remaining_quantity", "cost_basis", "open", "updated_at"])
        LotConsumption.objects.create(  # proceeds = cost → zero realized gain
            sale_txn=txn, lot=lot, quantity=take, cost=cost, proceeds=cost
        )
        carry.append(
            {"acquired_date": acquired.isoformat(), "quantity": str(take), "cost": str(cost)}
        )
    txn.lot_carry = carry
    return ZERO


def _apply_in_kind_in(txn) -> None:
    """Recreate the transferred lots from the snapshot, preserving each lot's original acquisition
    date + cost basis exactly (multiple carry entries → multiple lots)."""
    for e in (txn.lot_carry or []):
        qty = _q_qty(Decimal(str(e["quantity"])))
        cost = _q_amount(Decimal(str(e["cost"])))
        Lot.objects.create(
            account=txn.account,
            security=txn.security,
            acquired_date=datetime.date.fromisoformat(e["acquired_date"]),
            original_quantity=qty,
            remaining_quantity=qty,
            original_cost=cost,
            cost_basis=cost,
            open=qty > ZERO,
            source_txn=txn,
        )


def _apply_lot_effect(txn) -> Decimal:
    """Apply a transaction's lot effect during a replay; return its realized gain (0 if n/a)."""
    t = txn.txn_type
    if t == InvTxnType.BUY:
        _create_lot(txn, _q_amount(txn.amount + txn.fee))  # commission capitalized into basis
        return ZERO
    if t == InvTxnType.DIVIDEND_REINVEST:
        _create_lot(txn, _q_amount(txn.amount))
        return ZERO
    if t == InvTxnType.OPENING and txn.security_id:
        _create_lot(txn, _q_amount(txn.amount))
        return ZERO
    if t == InvTxnType.SELL:
        return _consume_lots(txn)
    if t == InvTxnType.SPLIT:
        _apply_split(txn)
        return ZERO
    if t == InvTxnType.RETURN_OF_CAPITAL:
        return _apply_return_of_capital(txn)
    if t == InvTxnType.IN_KIND_OUT:
        return _apply_in_kind_out(txn)
    if t == InvTxnType.IN_KIND_IN:
        _apply_in_kind_in(txn)
        return ZERO
    if t == InvTxnType.WORTHLESS:
        return _apply_worthless(txn)
    if t == InvTxnType.CASH_MERGER:
        return _apply_cash_merger(txn)
    return ZERO


@dataclass
class RebuildResult:
    """Outcome of a register replay: which entries need re-posting downstream."""
    resell_ids: list[int]      # SELL / return-of-capital (etc.) whose realized gain shifted
    resync_out_ids: list[int]  # IN_KIND_OUT legs whose materialized lot_carry snapshot changed


# Types whose realized gain, if it shifts on replay, requires re-posting their GL entry.
_GAIN_TYPES = frozenset({
    InvTxnType.SELL, InvTxnType.RETURN_OF_CAPITAL,
    InvTxnType.WORTHLESS, InvTxnType.CASH_MERGER,
})


def rebuild_account_lots(account) -> RebuildResult:
    """Wipe and replay the account's register in date order, rebuilding all lots + each
    disposition's realized gain + each in-kind-out's materialized snapshot. Returns the txns
    needing a re-post."""
    before = {t.id: (t.realized_gain, t.lot_carry) for t in account.transactions.all()}
    LotConsumption.objects.filter(sale_txn__account=account).delete()
    Lot.objects.filter(account=account).delete()

    resell: list[int] = []
    resync: list[int] = []
    for txn in account.transactions.order_by("date", "id"):
        rg = _q_amount(_apply_lot_effect(txn))
        fields = []
        if txn.realized_gain != rg:
            txn.realized_gain = rg
            fields.append("realized_gain")
        prev_carry = before.get(txn.id, (None, None))[1]
        if txn.txn_type == InvTxnType.IN_KIND_OUT and prev_carry != txn.lot_carry:
            fields.append("lot_carry")
            resync.append(txn.id)
        if fields:
            txn.save(update_fields=[*fields, "updated_at"])
        if txn.txn_type in _GAIN_TYPES and before.get(txn.id, (ZERO, None))[0] != rg:
            resell.append(txn.id)
    return RebuildResult(resell, resync)


# --- Posting ---------------------------------------------------------------------------------

def _external_key(txn) -> str:
    return f"investments:txn:{txn.pk}:v{txn.posting_version}"


def _description(txn) -> str:
    label = txn.type_label
    if txn.security_id:
        label = f"{label} {txn.security.display}"
    return f"{txn.account.nickname}: {label}"


def _lines_for(txn) -> list[LineInput]:
    """The balanced debit/credit pair for a transaction, or [] when it is cost-neutral in the GL
    (buys, splits, and zero-gain sells / returns-of-capital move only cash↔securities at cost)."""
    gl = ensure_gl_account(txn.account)
    amount = _q_amount(txn.amount)
    cur = txn.account.currency
    payee = {"person": txn.payee_person, "organization": txn.payee_organization}
    acct = txn.account

    def line(account, *, debit=ZERO, credit=ZERO, **party):
        return LineInput(account, debit=debit, credit=credit, currency=cur, **party)

    t = txn.txn_type
    if t == InvTxnType.OPENING:
        # amount = opening cash (security null) or opening-holding cost (security set); both bring
        # value into the tracked books against opening equity.
        return [line(gl, debit=amount), line(OPENING_EQUITY, credit=amount)]
    if t == InvTxnType.CONTRIBUTION:
        contra = txn.category_account or resolve_account(OPENING_EQUITY)
        return [line(gl, debit=amount), line(contra, credit=amount, **payee)]
    if t == InvTxnType.WITHDRAWAL:
        contra = txn.category_account or resolve_account(OPENING_EQUITY)
        return [line(contra, debit=amount, **payee), line(gl, credit=amount)]
    if t == InvTxnType.TRANSFER_IN:
        return [line(gl, debit=amount), line(TRANSFER_CLEARING, credit=amount)]
    if t == InvTxnType.TRANSFER_OUT:
        return [line(TRANSFER_CLEARING, debit=amount), line(gl, credit=amount)]
    if t in (InvTxnType.DIVIDEND, InvTxnType.DIVIDEND_REINVEST):
        contra = txn.category_account or resolve_posting_account(
            acct, "dividend_income", DIVIDEND_INCOME
        )
        return [line(gl, debit=amount), line(contra, credit=amount, **payee)]
    if t == InvTxnType.INTEREST:
        contra = txn.category_account or resolve_posting_account(
            acct, "investment_interest", INVEST_INTEREST
        )
        return [line(gl, debit=amount), line(contra, credit=amount, **payee)]
    if t == InvTxnType.CAP_GAIN_DIST:
        contra = txn.category_account or resolve_posting_account(
            acct, "capital_gains_distribution", CAPGAIN_DIST
        )
        return [line(gl, debit=amount), line(contra, credit=amount, **payee)]
    if t == InvTxnType.FEE:
        contra = txn.category_account or resolve_posting_account(acct, "fee_expense", INVEST_FEES)
        return [line(contra, debit=amount, **payee), line(gl, credit=amount)]
    if t in (InvTxnType.IN_KIND_IN, InvTxnType.IN_KIND_OUT):
        # Securities move at cost. Internal transfers net via 1150 clearing (the paired leg posts
        # the other side); external transfers cross the household boundary against opening equity.
        cost = _carry_total(txn.lot_carry)
        if cost <= ZERO:
            return []
        contra = (
            resolve_account(TRANSFER_CLEARING)
            if txn.counter_investment_account_id
            else resolve_account(OPENING_EQUITY)
        )
        if t == InvTxnType.IN_KIND_IN:
            return [line(gl, debit=cost), line(contra, credit=cost)]
        return [line(contra, debit=cost), line(gl, credit=cost)]
    if t in (InvTxnType.SELL, InvTxnType.RETURN_OF_CAPITAL,
             InvTxnType.WORTHLESS, InvTxnType.CASH_MERGER):
        # Only the realized gain/loss hits the ledger — the gl node already carries the position at
        # cost, so a disposition changes it by exactly (proceeds − cost). Cash-merger's cash comes
        # in via `signed_cash`; worthless has no cash (a pure basis write-off → capital loss).
        gain = _q_amount(txn.realized_gain)
        if gain > ZERO:
            return [line(gl, debit=gain), line(REALIZED_GAIN, credit=gain)]
        if gain < ZERO:
            g = -gain
            return [line(REALIZED_GAIN, debit=g), line(gl, credit=g)]
        return []
    if t in (InvTxnType.BUY, InvTxnType.SPLIT):
        return []
    raise ValueError(f"Unknown transaction type {t!r}")


def post_transaction(txn, *, user=None):
    """Post a saved transaction (skipping cost-neutral ones) and link the entry back onto it."""
    lines = _lines_for(txn)
    if len(lines) < 2:
        return None
    entry_type = (
        JournalEntry.EntryType.OPENING
        if txn.txn_type == InvTxnType.OPENING
        else JournalEntry.EntryType.STANDARD
    )
    entry = post_entry(
        date=txn.date,
        lines=lines,
        entry_type=entry_type,
        currency=txn.account.currency,
        source=txn,
        external_key=_external_key(txn),
        description=_description(txn),
        memo=txn.memo,
        reference=txn.reference,
        user=user,
    )
    if txn.journal_entry_id != entry.pk:
        txn.journal_entry = entry
        txn.save(update_fields=["journal_entry", "updated_at"])
    return entry


def repost_transaction(txn, *, user=None):
    """Reverse the current entry and post a fresh one (edit path; posted entries are immutable)."""
    current = txn.journal_entry
    if current is not None and current.status == JournalEntry.Status.POSTED:
        reverse_entry(current, user=user)
    txn.journal_entry = None
    txn.posting_version += 1
    txn.save(update_fields=["journal_entry", "posting_version", "updated_at"])
    return post_transaction(txn, user=user)


def unpost_transaction(txn, *, user=None) -> None:
    """Reverse the transaction's entry (used when a transaction is deleted); balances net out."""
    current = txn.journal_entry
    if current is not None and current.status == JournalEntry.Status.POSTED:
        reverse_entry(current, user=user)


# --- Orchestration (called by views after any register mutation) -----------------------------

def _repost_shifted(ids, *, exclude=None, user=None) -> None:
    """Repost each disposition whose realized gain shifted on replay (skipping `exclude`)."""
    for tid in ids:
        if tid == exclude:
            continue
        other = InvestmentTransaction.objects.filter(id=tid).first()
        if other is not None:
            repost_transaction(other, user=user)


def _resync_out_legs(ids, *, user=None, seen=None) -> None:
    """Re-sync the managed mirror of each in-kind-out leg whose materialized snapshot changed."""
    seen = seen if seen is not None else set()
    for tid in ids:
        if tid in seen:
            continue
        seen.add(tid)
        out = InvestmentTransaction.objects.filter(id=tid).first()
        if out is not None:
            sync_in_kind_pair(out, user=user, seen=seen)


@transaction.atomic
def apply_transaction(txn, *, user=None, is_new=True):
    """Rebuild lots (so realized gains + in-kind snapshots are current), post/repost this txn,
    re-post any other disposition whose realized gain shifted, and sync the mirror leg of any
    internal in-kind-out whose materialized snapshot changed (including this one on create/edit)."""
    result = rebuild_account_lots(txn.account)
    txn.refresh_from_db()
    if is_new:
        post_transaction(txn, user=user)
    else:
        repost_transaction(txn, user=user)
    _repost_shifted(result.resell_ids, exclude=txn.id, user=user)
    _resync_out_legs(result.resync_out_ids, user=user)


@transaction.atomic
def remove_transaction(txn, *, user=None, seen=None):
    """Reverse + soft-delete a transaction, rebuild lots, re-post affected dispositions, sync any
    affected in-kind-out mirrors, and (for an internal in-kind-out) remove its managed IN leg."""
    account = txn.account
    pair = txn.paired_txn if txn.txn_type == InvTxnType.IN_KIND_OUT else None
    unpost_transaction(txn, user=user)
    txn.delete()
    result = rebuild_account_lots(account)
    _repost_shifted(result.resell_ids, user=user)
    _resync_out_legs(result.resync_out_ids, user=user, seen=seen)
    if pair is not None:
        remove_transaction(pair, user=user, seen=seen)


def sync_in_kind_pair(out, *, user=None, seen=None):
    """Maintain the managed mirror IN leg of an *internal* in-kind-out transfer: create it, keep its
    lot snapshot / security / date in sync, or remove it if the transfer became external. The OUT
    leg is authoritative (it materializes `lot_carry` from the lots actually consumed); the IN leg
    is a mirror in the destination account so the 1150 clearing account nets to zero across the two.

    Bounded on purpose: it rebuilds the destination and reposts *its* dispositions, but does not
    chain into the destination's own outgoing transfers — each transfer stays correct when its own
    OUT leg is created/edited. `seen` guards against re-entrancy in cyclic transfer graphs."""
    if out.txn_type != InvTxnType.IN_KIND_OUT:
        return
    seen = seen if seen is not None else set()
    dest = out.counter_investment_account
    pair = out.paired_txn

    if dest is None:
        # External now (or changed to external) — drop any stale mirror.
        if pair is not None:
            _unlink_pair(out)
            remove_transaction(pair, user=user, seen=seen)
        return

    # A pair living in the wrong account (destination changed) is stale — drop and recreate.
    if pair is not None and pair.account_id != dest.id:
        _unlink_pair(out)
        remove_transaction(pair, user=user, seen=seen)
        pair = None
    if pair is None:
        pair = InvestmentTransaction(account=dest, txn_type=InvTxnType.IN_KIND_IN)

    pair.security = out.security
    pair.date = out.date
    pair.quantity = out.quantity
    pair.amount = ZERO
    pair.counter_investment_account = out.account
    pair.lot_carry = out.lot_carry
    pair.memo = out.memo
    pair.reference = out.reference
    pair.save()

    if out.paired_txn_id != pair.id:
        out.paired_txn = pair
        out.save(update_fields=["paired_txn", "updated_at"])
    if pair.paired_txn_id != out.id:
        pair.paired_txn = out
        pair.save(update_fields=["paired_txn", "updated_at"])

    result = rebuild_account_lots(dest)
    pair.refresh_from_db()
    if pair.journal_entry_id is not None:
        repost_transaction(pair, user=user)
    else:
        post_transaction(pair, user=user)
    _repost_shifted(result.resell_ids, exclude=pair.id, user=user)


def _unlink_pair(out) -> None:
    if out.paired_txn_id is not None:
        out.paired_txn = None
        out.save(update_fields=["paired_txn", "updated_at"])


def create_matching_leg(txn, *, user=None):
    """For a cash transfer against a tracked bank account, post the opposite banking leg so the 1150
    clearing account nets to zero across the two modules."""
    if txn.counter_account_id is None or txn.txn_type not in (
        InvTxnType.TRANSFER_IN,
        InvTxnType.TRANSFER_OUT,
    ):
        return None
    from apps.banking.models import BankTransaction
    from apps.banking.models import TxnType as BankTxnType
    from apps.banking.services import post_transaction as bank_post

    bank_type = (
        BankTxnType.TRANSFER_OUT
        if txn.txn_type == InvTxnType.TRANSFER_IN
        else BankTxnType.TRANSFER_IN
    )
    leg = BankTransaction.objects.create(
        account=txn.counter_account,
        txn_type=bank_type,
        date=txn.date,
        amount=txn.amount,
        counter_external=f"{txn.account.nickname} transfer",
        memo=txn.memo,
        reference=txn.reference,
    )
    bank_post(leg, user=user)
    return leg


def sync_holder_p2o(account, *, user=None) -> None:
    """Ensure each holder has an 'Account Holder' P2O link to the institution. Add-only."""
    from apps.relationships.models import PersonOrgRelationship, PersonOrgRelationshipType

    rtype = PersonOrgRelationshipType.objects.filter(code="account_holder").first()
    if rtype is None:
        return
    for holder in account.holders.all():
        PersonOrgRelationship.objects.get_or_create(
            person=holder.person, organization=account.institution, type=rtype
        )


# --- Read models -----------------------------------------------------------------------------

def cash_balance(account) -> Decimal:
    """Settlement cash held in the account (from the register)."""
    return _q_amount(sum((t.signed_cash for t in account.transactions.all()), ZERO))


def cost_basis(account) -> Decimal:
    """Total cost basis of the account's open lots."""
    total = account.lots.filter(open=True).aggregate(s=Sum("cost_basis"))["s"]
    return _q_amount(total or ZERO)


@dataclass
class Holding:
    security: Security
    quantity: Decimal
    cost_basis: Decimal
    market_value: Decimal
    price: Decimal | None

    @property
    def avg_cost(self) -> Decimal:
        return _q_amount(self.cost_basis / self.quantity) if self.quantity else ZERO

    @property
    def unrealized_gain(self) -> Decimal:
        return _q_amount(self.market_value - self.cost_basis)

    @property
    def unrealized_pct(self) -> Decimal:
        return _q_amount(self.unrealized_gain / self.cost_basis * 100) if self.cost_basis else ZERO


def holdings(account) -> list[Holding]:
    """Per-security open positions in an account (quantity, cost, market value)."""
    rows: dict[int, dict] = {}
    for lot in account.lots.filter(open=True).select_related("security"):
        r = rows.setdefault(
            lot.security_id, {"security": lot.security, "qty": ZERO, "cost": ZERO}
        )
        r["qty"] += lot.remaining_quantity
        r["cost"] += lot.cost_basis
    out: list[Holding] = []
    for r in rows.values():
        qty = _q_qty(r["qty"])
        if qty <= ZERO:
            continue
        cost = _q_amount(r["cost"])
        price = r["security"].latest_price
        mv = _q_amount(qty * price) if price is not None else cost
        out.append(Holding(security=r["security"], quantity=qty, cost_basis=cost,
                            market_value=mv, price=price))
    out.sort(key=lambda h: h.market_value, reverse=True)
    return out


def market_value(account) -> Decimal:
    """Market value of the account's securities (excludes settlement cash)."""
    return _q_amount(sum((h.market_value for h in holdings(account)), ZERO))


@dataclass
class Slice:
    label: str
    value: Decimal
    tint: str

    def pct_of(self, total) -> Decimal:
        return _q_amount(self.value / total * 100) if total else ZERO


def allocation(accounts=None, *, by: str = "asset_class") -> list[Slice]:
    """Portfolio market value grouped for the dashboard donut/bars: by asset class, account group,
    or institution. Settlement cash is folded in as its own 'Cash' slice."""
    from apps.investments.models import ASSET_CLASS_TINT, AssetClass

    if accounts is None:
        accounts = list(InvestmentAccount.objects.select_related("institution"))
    buckets: dict[str, dict] = {}

    def add(key, label, tint, value):
        if value <= ZERO:
            return
        b = buckets.setdefault(key, {"label": label, "tint": tint, "value": ZERO})
        b["value"] = _q_amount(b["value"] + value)

    total_cash = ZERO
    for acct in accounts:
        total_cash += acct.cash_balance
        for h in holdings(acct):
            if by == "asset_class":
                add(h.security.asset_class, h.security.asset_class_label, h.security.tint,
                    h.market_value)
            elif by == "group":
                add(acct.group, acct.group_label, acct.group_tint, h.market_value)
            else:  # institution
                add(str(acct.institution_id), acct.institution.display,
                    acct.institution.avatar_tint, h.market_value)
    # Cash slice
    if total_cash > ZERO:
        if by == "asset_class":
            cash_tint = ASSET_CLASS_TINT[AssetClass.CASH]
            add(AssetClass.CASH, AssetClass.CASH.label, cash_tint, total_cash)
        elif by == "group":
            add("cash", "Cash", "slate", total_cash)
        else:
            add("cash", "Cash", "slate", total_cash)

    slices = [Slice(label=b["label"], value=b["value"], tint=b["tint"]) for b in buckets.values()]
    slices.sort(key=lambda s: s.value, reverse=True)
    return slices


DONUT_RADIUS = 52
DONUT_CIRC = 326.7256  # 2 · π · 52, precomputed (templates can't do arithmetic)


def donut_segments(slices) -> list[dict]:
    """Precompute stroke-dasharray/offset arcs for the c-donut SVG from a list of Slices."""
    total = sum((s.value for s in slices), ZERO)
    segs = []
    cum = 0.0
    for s in slices:
        frac = float(s.value) / float(total) if total else 0.0
        length = frac * DONUT_CIRC
        segs.append({
            "tint": s.tint,
            "label": s.label,
            "value": s.value,
            "pct": s.pct_of(total),
            "dasharray": f"{length:.3f} {DONUT_CIRC - length:.3f}",
            "dashoffset": f"{-cum * DONUT_CIRC:.3f}",
        })
        cum += frac
    return segs


def register(account) -> list[dict]:
    """The account's transactions with a running settlement-cash balance, newest-first."""
    running = ZERO
    rows = []
    for txn in account.transactions.order_by("date", "id"):
        running = _q_amount(running + txn.signed_cash)
        rows.append({"txn": txn, "balance": running})
    rows.reverse()
    return rows


def total_portfolio_value() -> Decimal:
    """Total market value (securities + cash) across every account — base/native assumed equal."""
    return _q_amount(sum((a.total_value for a in InvestmentAccount.objects.all()), ZERO))


def _income_ytd(today) -> Decimal:
    year_start = datetime.date(today.year, 1, 1)
    total = InvestmentTransaction.objects.filter(
        date__gte=year_start,
        txn_type__in=[
            InvTxnType.DIVIDEND, InvTxnType.DIVIDEND_REINVEST,
            InvTxnType.INTEREST, InvTxnType.CAP_GAIN_DIST,
        ],
    ).aggregate(s=Sum("amount"))["s"]
    return _q_amount(total or ZERO)


def upcoming_maturities(within_days: int = 365):
    """CD/term-deposit securities the household still holds, maturing within the window."""
    today = datetime.date.today()
    horizon = today + datetime.timedelta(days=within_days)
    held_ids = set(
        Lot.objects.filter(open=True).values_list("security_id", flat=True)
    )
    return list(
        Security.objects.filter(
            kind="cd", maturity_date__isnull=False,
            maturity_date__gte=today, maturity_date__lte=horizon, id__in=held_ids,
        ).order_by("maturity_date")
    )


def dashboard_stats() -> dict:
    """Headline figures + drill-down feeds for the Investments dashboard."""
    today = datetime.date.today()
    accounts = list(InvestmentAccount.objects.select_related("institution"))

    total_cash = _q_amount(sum((a.cash_balance for a in accounts), ZERO))
    total_cost = _q_amount(sum((a.cost_basis for a in accounts), ZERO))
    total_market = _q_amount(sum((a.market_value for a in accounts), ZERO))
    total_value = _q_amount(total_market + total_cash)
    unrealized = _q_amount(total_market - total_cost)

    # Top holdings across all accounts (aggregate the same security across accounts).
    agg: dict[int, dict] = {}
    for acct in accounts:
        for h in holdings(acct):
            r = agg.setdefault(
                h.security.id,
                {"security": h.security, "qty": ZERO, "cost": ZERO, "mv": ZERO},
            )
            r["qty"] += h.quantity
            r["cost"] += h.cost_basis
            r["mv"] += h.market_value
    top = sorted(agg.values(), key=lambda r: r["mv"], reverse=True)[:6]

    recent = list(
        InvestmentTransaction.objects.select_related("account", "security").order_by(
            "-date", "-id"
        )[:8]
    )

    return {
        "accounts_count": len(accounts),
        "institutions_count": len({a.institution_id for a in accounts}),
        "holdings_count": Lot.objects.filter(open=True)
        .values("account_id", "security_id").distinct().count(),
        "total_value": total_value,
        "total_cost": total_cost,
        "total_cash": total_cash,
        "total_market": total_market,
        "unrealized": unrealized,
        "income_ytd": _income_ytd(today),
        "accounts": accounts,
        "allocation": allocation(accounts, by="asset_class"),
        "by_group": allocation(accounts, by="group"),
        "top_holdings": top,
        "recent": recent,
        "maturities": upcoming_maturities(),
    }
