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

import bisect
import datetime
from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.db.models.functions import ExtractYear

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
    OptionRight,
    Security,
    SecurityPrice,
    VestingGrant,
)

# --- Fixed contras / remappable activities ---------------------------------------------------

OPENING_EQUITY = "opening_balance_equity"      # 3100
TRANSFER_CLEARING = "transfer_clearing"        # 1150
DIVIDEND_INCOME = "dividend_income"            # 4310
REALIZED_GAIN = "realized_capital_gain"        # 4320 (gains credit, losses debit)
CAPGAIN_DIST = "capital_gains_distribution"    # 4330
INVEST_INTEREST = "investment_interest"        # 4340
INVEST_FEES = "investment_fees"                # 5870
INTEREST_EXPENSE = "interest_expense"          # 5860 (margin interest)
SUBSTITUTE_DIVIDEND_EXPENSE = "substitute_dividend_expense"  # 5880 (dividends paid on a short)

# Category legs the Expert-mode Accounting Setup tab can remap, per investment account. Structural
# legs (opening equity, transfer clearing, realized-gain) are never remappable.
POSTING_ACTIVITIES = [
    {"key": "dividend_income", "label": "Dividends", "kind": "income", "default": DIVIDEND_INCOME},
    {"key": "investment_interest", "label": "Interest", "kind": "income",
     "default": INVEST_INTEREST},
    {"key": "capital_gains_distribution", "label": "Capital-gain distributions", "kind": "income",
     "default": CAPGAIN_DIST},
    {"key": "fee_expense", "label": "Fees", "kind": "expense", "default": INVEST_FEES},
    {"key": "margin_interest_expense", "label": "Margin interest", "kind": "expense",
     "default": INTEREST_EXPENSE},
    {"key": "substitute_dividend_expense", "label": "Payments in lieu (short dividends)",
     "kind": "expense", "default": SUBSTITUTE_DIVIDEND_EXPENSE},
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
#
# The engine replays the register through `_apply_lot_effect`, touching lots ONLY via an injected
# `store` (a LotStore). `DbLotStore` is the live path (persists Lot/LotConsumption — behaviour
# identical to before this abstraction). `MemLotStore` is a non-mutating in-memory path the
# value-over-time overlay uses to reconstruct holdings as of a past date, without writing anything.

def _sid(security):
    """Normalize a Security instance or pk to a pk."""
    return security.pk if hasattr(security, "pk") else security


def _open_lots(account, security):
    return list(
        Lot.objects.filter(account=account, security=security, open=True).order_by(
            "acquired_date", "id"
        )
    )


class DbLotStore:
    """The live lot store: reads/writes the real Lot + LotConsumption tables (unchanged)."""

    def __init__(self, account):
        self.account = account

    def open_lots(self, security):
        return _open_lots(self.account, security)

    def add_lot(self, *, security_id, acquired_date, original_quantity, remaining_quantity,
                original_cost, cost_basis, open, source_txn):
        return Lot.objects.create(
            account=self.account, security_id=security_id, acquired_date=acquired_date,
            original_quantity=original_quantity, remaining_quantity=remaining_quantity,
            original_cost=original_cost, cost_basis=cost_basis, open=open, source_txn=source_txn,
        )

    def save(self, lot, fields):
        lot.save(update_fields=[*fields, "updated_at"])

    def record_consumption(self, *, sale_txn, lot, quantity, cost, proceeds):
        LotConsumption.objects.create(
            sale_txn=sale_txn, lot=lot, quantity=quantity, cost=cost, proceeds=proceeds
        )


@dataclass
class _MemLot:
    """In-memory stand-in for a Lot row (value-over-time reconstruction only)."""
    security_id: int
    acquired_date: datetime.date
    original_quantity: Decimal
    remaining_quantity: Decimal
    original_cost: Decimal
    cost_basis: Decimal
    open: bool
    source_txn_id: int
    seq: int  # insertion order — mirrors the DB autoincrement id used as the FIFO tie-break


class MemLotStore:
    """A non-mutating, in-memory lot store. The SAME engine handlers build lots here during a
    date-bounded replay, so reconstructed as-of-date holdings match a live rebuild exactly — with
    no DB writes. `save`/`record_consumption` are no-ops (MemLots are mutated in place)."""

    def __init__(self):
        self._by_sec: dict[int, list[_MemLot]] = {}
        self._seq = 0

    def open_lots(self, security):
        lots = [lot for lot in self._by_sec.get(_sid(security), []) if lot.open]
        lots.sort(key=lambda lot: (lot.acquired_date, lot.seq))
        return lots

    def add_lot(self, *, security_id, acquired_date, original_quantity, remaining_quantity,
                original_cost, cost_basis, open, source_txn):
        lot = _MemLot(
            security_id=security_id, acquired_date=acquired_date,
            original_quantity=_q_qty(original_quantity),
            remaining_quantity=_q_qty(remaining_quantity),
            original_cost=_q_amount(original_cost), cost_basis=_q_amount(cost_basis),
            open=open, source_txn_id=source_txn.pk, seq=self._seq,
        )
        self._seq += 1
        self._by_sec.setdefault(security_id, []).append(lot)
        return lot

    def save(self, lot, fields):
        pass

    def record_consumption(self, *, sale_txn, lot, quantity, cost, proceeds):
        pass

    def open_positions(self):
        """Aggregate open lots into {security_id: (quantity, cost)} — for as-of-date holdings.
        Drops fully-flat (0, 0) positions, matching the live `holdings()` read model."""
        out: dict[int, list[Decimal]] = {}
        for lots in self._by_sec.values():
            for lot in lots:
                if not lot.open:
                    continue
                agg = out.setdefault(lot.security_id, [ZERO, ZERO])
                agg[0] += lot.remaining_quantity
                agg[1] += lot.cost_basis
        result: dict[int, tuple] = {}
        for sid, (q, c) in out.items():
            qq, cc = _q_qty(q), _q_amount(c)
            if qq != ZERO or cc != ZERO:
                result[sid] = (qq, cc)
        return result


def _plan_draws(txn, store, security=None, qty=None) -> list[tuple]:
    """Which LONG lots (and how much of each) a disposition draws from — FIFO by default, else the
    specific lots the user chose (keyed by source buy txn, which survives a replay). `security` and
    `qty` default to the txn's; option exercise/assignment passes the underlying + its shares."""
    security = security or txn.security
    qty_needed = _q_qty(txn.quantity if qty is None else qty)
    open_lots = [lot for lot in store.open_lots(security) if lot.remaining_quantity > ZERO]

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


def _consume_draws(txn, store, draws, net_proceeds) -> Decimal:
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
        store.save(lot, ["remaining_quantity", "cost_basis", "open"])
        store.record_consumption(sale_txn=txn, lot=lot, quantity=take, cost=cost, proceeds=proceeds)
        total_cost = _q_amount(total_cost + cost)
    return _q_amount(net_proceeds - total_cost)


def _consume_lots(txn, store) -> Decimal:
    """Draw the sale's quantity from lots (FIFO or specific); return the realized gain."""
    return _consume_draws(txn, store, _plan_draws(txn, store), txn.net_proceeds)


def _plan_short_draws(txn, store, security=None, qty=None) -> list[tuple]:
    """Which SHORT (negative-quantity) lots a buy-to-cover / buy-to-close draws from — FIFO (oldest
    short first) by default, else the specific short lots the user chose (keyed by the opening txn,
    which survives a replay). `security`/`qty` default to the txn's; `qty` is the positive count to
    buy back."""
    security = security or txn.security
    qty_needed = _q_qty(txn.quantity if qty is None else qty)
    short_lots = [lot for lot in store.open_lots(security) if lot.remaining_quantity < ZERO]

    draws: list[tuple[Lot, Decimal]] = []
    if txn.cost_basis_method == "specific" and txn.lot_selection:
        by_src = {lot.source_txn_id: lot for lot in short_lots}
        for sel in txn.lot_selection:
            lot = by_src.get(sel.get("buy_txn"))
            take = _q_qty(sel.get("qty", 0))
            if lot is None or take <= ZERO or take > -lot.remaining_quantity:
                raise InsufficientShares(
                    f"Short lot for {security} unavailable or insufficient to cover."
                )
            draws.append((lot, take))
        if _q_qty(sum(q for _, q in draws)) != qty_needed:
            raise InsufficientShares("Selected lots do not sum to the cover quantity.")
        return draws

    remaining = qty_needed
    for lot in short_lots:
        if remaining <= ZERO:
            break
        take = min(-lot.remaining_quantity, remaining)  # magnitude available in this short lot
        if take > ZERO:
            draws.append((lot, _q_qty(take)))
            remaining = _q_qty(remaining - take)
    if remaining > ZERO:
        raise InsufficientShares(
            f"Not enough short {security} to buy back {qty_needed} (short by {remaining})."
        )
    return draws


def _consume_short(txn, store, draws, cash_out) -> Decimal:
    """Close the given SHORT lot draws, paying `cash_out` to buy the shares back (recording a
    LotConsumption per draw), and return the realized gain (short proceeds released − cash paid).
    Shared by buy-to-cover, option buy-to-close and the short side of an option expiry.

    A short lot carries `remaining_quantity < 0` and `cost_basis < 0` (credit = proceeds owed). The
    close condition is `remaining >= 0` — a full cover lands at exactly 0 (the OPPOSITE of the long
    path's `<= 0` in `_consume_draws`; this is the most error-prone line in the engine)."""
    cash_out = _q_amount(cash_out)
    total_qty = _q_qty(sum((q for _, q in draws), ZERO))
    total_proceeds = ZERO
    allocated = ZERO
    n = len(draws)
    for i, (lot, take) in enumerate(draws):
        mag = -lot.remaining_quantity  # positive open short magnitude
        if mag and mag != ZERO:
            proceeds = _q_amount(-lot.cost_basis * (take / mag))  # short proceeds released
        else:
            proceeds = ZERO
        if i == n - 1:
            cost = _q_amount(cash_out - allocated)  # last draw absorbs the rounding remainder
        else:
            cost = _q_amount(cash_out * (take / total_qty)) if total_qty else ZERO
        allocated = _q_amount(allocated + cost)

        lot.remaining_quantity = _q_qty(lot.remaining_quantity + take)  # −Q + q → toward 0
        lot.cost_basis = _q_amount(lot.cost_basis + proceeds)           # −P + P·q/Q → toward 0
        if lot.remaining_quantity >= ZERO:
            lot.remaining_quantity = ZERO
            lot.cost_basis = ZERO
            lot.open = False
        store.save(lot, ["remaining_quantity", "cost_basis", "open"])
        store.record_consumption(sale_txn=txn, lot=lot, quantity=take, cost=cost, proceeds=proceeds)
        total_proceeds = _q_amount(total_proceeds + proceeds)
    return _q_amount(total_proceeds - cash_out)


def _all_open_draws(txn, store) -> list[tuple]:
    """Every open LONG lot of the transaction's security, drawn in full — for whole-position events
    (worthless write-off, cash buyout/merger). Short lots are excluded; disposing a short position
    through these corporate-action events is unsupported (close it with a buy-to-cover instead)."""
    return [
        (lot, lot.remaining_quantity)
        for lot in store.open_lots(txn.security)
        if lot.remaining_quantity > ZERO
    ]


def _apply_worthless(txn, store) -> Decimal:
    """Write the entire position off: dispose every open lot at cost for zero proceeds, realizing a
    capital loss equal to the remaining basis. Cash-neutral."""
    return _consume_draws(txn, store, _all_open_draws(txn, store), ZERO)


def _apply_cash_merger(txn, store) -> Decimal:
    """Cash buyout of the whole position: dispose every open lot for the buyout cash, realizing the
    gain/loss (a full sell whose proceeds are the cash received)."""
    return _consume_draws(txn, store, _all_open_draws(txn, store), _q_amount(txn.amount))


def _apply_split(txn, store) -> None:
    if not (txn.split_ratio_new and txn.split_ratio_old):
        return
    ratio = txn.split_ratio_new / txn.split_ratio_old
    for lot in store.open_lots(txn.security):
        lot.remaining_quantity = _q_qty(lot.remaining_quantity * ratio)
        lot.original_quantity = _q_qty(lot.original_quantity * ratio)
        store.save(lot, ["remaining_quantity", "original_quantity"])


def _apply_merger(txn, store) -> None:
    """Stock-for-stock merger: each open lot of the original security (`txn.security` = X) becomes a
    lot of `txn.target_security` (Y) at the exchange ratio (Y shares per X share), carrying cost
    basis and acquisition date over unchanged. Nothing is realized; total basis is preserved."""
    if not (txn.split_ratio_new and txn.split_ratio_old and txn.target_security_id):
        return
    ratio = txn.split_ratio_new / txn.split_ratio_old
    for lot in store.open_lots(txn.security):
        qty = _q_qty(lot.remaining_quantity * ratio)
        store.add_lot(
            security_id=txn.target_security_id,
            acquired_date=lot.acquired_date,
            original_quantity=qty,
            remaining_quantity=qty,
            original_cost=lot.cost_basis,
            cost_basis=lot.cost_basis,  # basis carries over entirely
            open=lot.cost_basis > ZERO or qty > ZERO,
            source_txn=txn,
        )
        lot.remaining_quantity = ZERO
        lot.cost_basis = ZERO
        lot.open = False
        store.save(lot, ["remaining_quantity", "cost_basis", "open"])


def _sell_fractional_remainder(txn, store, security, cash) -> Decimal:
    """Sell the aggregate fractional remainder of `security` for `cash` — cash-in-lieu of a
    fractional share (e.g. a broker pays cash for the leftover fraction of a spin-off entitlement).
    Rounds the total holding down to whole shares, drawing the fraction FIFO, and returns the
    realized gain (cash − the fraction's cost basis). No fraction → nothing sold, returns 0."""
    lots = [lot for lot in store.open_lots(security) if lot.remaining_quantity > ZERO]
    total = _q_qty(sum((lot.remaining_quantity for lot in lots), ZERO))
    fraction = _q_qty(total - (total // 1))          # 16.80278 → 0.80278
    if fraction <= ZERO:
        return ZERO
    draws: list[tuple] = []
    remaining = fraction
    for lot in lots:
        if remaining <= ZERO:
            break
        take = min(lot.remaining_quantity, remaining)
        if take > ZERO:
            draws.append((lot, _q_qty(take)))
            remaining = _q_qty(remaining - take)
    return _consume_draws(txn, store, draws, _q_amount(cash))


def _apply_spinoff(txn, store) -> Decimal:
    """Spin-off: allocate `basis_pct`% of each open X lot's basis to a new lot of `target_security`
    (Y); the parent (X) keeps the remainder. Distribute `ratio` Y shares per original X share; new Y
    lots inherit X's acquisition date (holding period tacks). X's share count is unchanged. Cost
    basis is conserved per lot (X_after + Y == X_before, exactly).

    If `amount` > 0 (cash received in lieu of the fractional Y share), the fractional remainder of Y
    is then sold for that cash and the realized gain returned — so the entitlement lands on whole
    shares + cash. Returns 0 for a plain (cash-neutral) spin-off."""
    if not (
        txn.split_ratio_new and txn.split_ratio_old
        and txn.target_security_id and txn.basis_pct is not None
    ):
        return ZERO
    dist = txn.split_ratio_new / txn.split_ratio_old
    f = txn.basis_pct / Decimal("100")
    for lot in store.open_lots(txn.security):
        alloc = _q_amount(lot.cost_basis * f)
        qty = _q_qty(lot.remaining_quantity * dist)
        store.add_lot(
            security_id=txn.target_security_id,
            acquired_date=lot.acquired_date,
            original_quantity=qty,
            remaining_quantity=qty,
            original_cost=alloc,
            cost_basis=alloc,
            open=alloc > ZERO or qty > ZERO,
            source_txn=txn,
        )
        lot.cost_basis = _q_amount(lot.cost_basis - alloc)
        store.save(lot, ["cost_basis"])
    cash = _q_amount(txn.amount)
    if cash > ZERO:
        return _sell_fractional_remainder(txn, store, txn.target_security, cash)
    return ZERO


def _apply_return_of_capital(txn, store) -> Decimal:
    """Reduce open-lot basis by the distribution; any excess over total basis is a realized gain."""
    lots = store.open_lots(txn.security)
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
            store.save(lot, ["cost_basis"])
        return ZERO
    # Basis exhausted — zero every lot and recognize the excess as a realized gain.
    for lot in lots:
        lot.cost_basis = ZERO
        store.save(lot, ["cost_basis"])
    return _q_amount(amount - total_basis)


def _make_lot(txn, store, security, qty, cost: Decimal, acquired) -> None:
    """Create a lot for `security` under `txn`'s account. `qty`/`cost` may be NEGATIVE — a short
    position (sell-short, written option) is a lot with negative quantity and a credit basis (the
    proceeds received = the buy-back obligation). `open` is true whenever the lot is non-flat."""
    qty = _q_qty(qty)
    store.add_lot(
        security_id=_sid(security),
        acquired_date=acquired,
        original_quantity=qty,
        remaining_quantity=qty,
        original_cost=cost,
        cost_basis=cost,
        open=qty != ZERO,
        source_txn=txn,
    )


def _create_lot(txn, store, cost: Decimal) -> None:
    if txn.security_id and not txn.security.track_lots:
        _pool_into_lot(txn, store, cost)
    else:
        _make_lot(txn, store, txn.security, txn.quantity, cost, txn.date)


def _pool_into_lot(txn, store, cost: Decimal) -> None:
    """Average-cost pooling for a non-lot-tracked security (e.g. a money-market fund): fold the
    buy / reinvest into the single existing open long lot rather than minting a new one, so the
    holding stays one blended-cost position instead of a lot per dividend. Falls back to a fresh lot
    when none is open yet. The pooled lot keeps its earliest acquisition date (holding period is
    irrelevant for a stable-value fund)."""
    qty = _q_qty(txn.quantity)
    cost = _q_amount(cost)
    pool = next((lot for lot in store.open_lots(txn.security) if lot.remaining_quantity > ZERO),
                None)
    if pool is None:
        _make_lot(txn, store, txn.security, qty, cost, txn.date)
        return
    pool.original_quantity = _q_qty(pool.original_quantity + qty)
    pool.remaining_quantity = _q_qty(pool.remaining_quantity + qty)
    pool.original_cost = _q_amount(pool.original_cost + cost)
    pool.cost_basis = _q_amount(pool.cost_basis + cost)
    pool.open = pool.remaining_quantity != ZERO
    store.save(pool, ["original_quantity", "remaining_quantity", "original_cost", "cost_basis",
                      "open"])


def _apply_in_kind_out(txn, store) -> Decimal:
    """Consume lots at cost (FIFO or specific), realizing NO gain, and materialize the consumed
    lots onto `txn.lot_carry` (persisted by `rebuild_account_lots`) so the paired IN leg can
    recreate them with their original acquisition date + cost basis."""
    draws = _plan_draws(txn, store)
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
        store.save(lot, ["remaining_quantity", "cost_basis", "open"])
        # proceeds = cost → zero realized gain
        store.record_consumption(sale_txn=txn, lot=lot, quantity=take, cost=cost, proceeds=cost)
        carry.append(
            {"acquired_date": acquired.isoformat(), "quantity": str(take), "cost": str(cost)}
        )
    txn.lot_carry = carry
    return ZERO


def _apply_in_kind_in(txn, store) -> None:
    """Recreate the transferred lots from the snapshot, preserving each lot's original acquisition
    date + cost basis exactly (multiple carry entries → multiple lots)."""
    for e in (txn.lot_carry or []):
        qty = _q_qty(Decimal(str(e["quantity"])))
        cost = _q_amount(Decimal(str(e["cost"])))
        store.add_lot(
            security_id=txn.security_id,
            acquired_date=datetime.date.fromisoformat(e["acquired_date"]),
            original_quantity=qty,
            remaining_quantity=qty,
            original_cost=cost,
            cost_basis=cost,
            open=qty > ZERO,
            source_txn=txn,
        )


# --- Options ---------------------------------------------------------------------------------

def _draws_cost(draws) -> Decimal:
    """Total remaining-basis of the given LONG draws (matches `_consume_draws`'s per-lot cost)."""
    return _q_amount(sum(
        (_q_amount(lot.cost_basis * (take / lot.remaining_quantity)) if lot.remaining_quantity
         else ZERO)
        for lot, take in draws
    ))


def _draws_credit(draws) -> Decimal:
    """Total credit (proceeds owed) of the given SHORT draws (matches `_consume_short`)."""
    return _q_amount(sum(
        (_q_amount(-lot.cost_basis * (take / -lot.remaining_quantity)) if lot.remaining_quantity
         else ZERO)
        for lot, take in draws
    ))


def _roll_out_option(txn, store, opt, qty) -> Decimal:
    """Close `qty` (shares-equivalent) of the option position at ZERO realized gain — the premium
    basis rolls into/out of the underlying rather than being recognized. A long option (positive
    lots) is consumed at its cost; a written option (negative lots) is closed at its credit. Returns
    the absolute premium basis released."""
    open_lots = store.open_lots(opt)
    is_long = any(lot.remaining_quantity > ZERO for lot in open_lots)
    if is_long:
        draws = _plan_draws(txn, store, security=opt, qty=qty)
        premium = _draws_cost(draws)
        _consume_draws(txn, store, draws, premium)      # proceeds = cost → zero gain
        return premium
    draws = _plan_short_draws(txn, store, security=opt, qty=qty)
    premium = _draws_credit(draws)
    _consume_short(txn, store, draws, premium)          # cash paid = credit → zero gain
    return premium


def _dispose_underlying(txn, store, underlying, qty, proceeds) -> Decimal:
    """Sell `qty` underlying shares for `proceeds` (total): consume held LONG lots first (realizing
    gain); if fewer are held than `qty`, open a SHORT lot for the shortfall (credit basis = its
    pro-rata share of the proceeds). Returns the realized gain from the long portion."""
    qty = _q_qty(qty)
    proceeds = _q_amount(proceeds)
    long_lots = [lt for lt in store.open_lots(underlying) if lt.remaining_quantity > ZERO]
    held = _q_qty(sum((lot.remaining_quantity for lot in long_lots), ZERO))
    pps = (proceeds / qty) if qty else ZERO
    long_take = min(held, qty)
    gain = ZERO
    long_proceeds = ZERO
    if long_take > ZERO:
        long_proceeds = _q_amount(pps * long_take)
        draws = _plan_draws(txn, store, security=underlying, qty=long_take)
        gain = _consume_draws(txn, store, draws, long_proceeds)
    short_qty = _q_qty(qty - long_take)
    if short_qty > ZERO:  # naked: create a short underlying lot for the shortfall
        short_cost = -_q_amount(proceeds - long_proceeds)
        _make_lot(txn, store, underlying, -short_qty, short_cost, txn.date)
    return gain


def _apply_exercise(txn, store) -> Decimal:
    """You exercise a LONG option. Roll the option out at zero gain, then affect the underlying:
    a call BUYS at strike (option premium capitalizes into the new lot's basis, posts nothing); a
    put SELLS at strike (premium reduces proceeds, realizes gain)."""
    opt = txn.security
    und = opt.underlying if opt else None
    if und is None:
        return ZERO
    qty, strike_cash, fee = _q_qty(txn.quantity), _q_amount(txn.amount), _q_amount(txn.fee)
    premium = _roll_out_option(txn, store, opt, qty)
    if opt.option_right == OptionRight.CALL:
        _make_lot(txn, store, und, qty, _q_amount(strike_cash + premium + fee), txn.date)
        return ZERO
    return _dispose_underlying(txn, store, und, qty, _q_amount(strike_cash - premium - fee))


def _apply_assign(txn, store) -> Decimal:
    """Your WRITTEN option is assigned. Roll the option out at zero gain, then hit the underlying:
    a call SELLS at strike (premium received adds to proceeds, realizes gain); a put BUYS at strike
    (premium reduces the new lot's basis, posts nothing)."""
    opt = txn.security
    und = opt.underlying if opt else None
    if und is None:
        return ZERO
    qty, strike_cash, fee = _q_qty(txn.quantity), _q_amount(txn.amount), _q_amount(txn.fee)
    premium = _roll_out_option(txn, store, opt, qty)
    if opt.option_right == OptionRight.CALL:
        return _dispose_underlying(txn, store, und, qty, _q_amount(strike_cash + premium - fee))
    _make_lot(txn, store, und, qty, _q_amount(strike_cash - premium + fee), txn.date)
    return ZERO


def _apply_option_expire(txn, store) -> Decimal:
    """The option expires worthless: dispose the whole position for nothing. A long option is a full
    loss (basis written off); a written option is a full gain (the premium is kept)."""
    open_lots = store.open_lots(txn.security)
    net = _q_qty(sum((lot.remaining_quantity for lot in open_lots), ZERO))
    if net > ZERO:
        return _consume_draws(txn, store, _all_open_draws(txn, store), ZERO)
    short_draws = [(lot, -lot.remaining_quantity) for lot in open_lots
                   if lot.remaining_quantity < ZERO]
    return _consume_short(txn, store, short_draws, ZERO)


def _apply_lot_effect(txn, store) -> Decimal:
    """Apply a transaction's lot effect during a replay; return its realized gain (0 if n/a).
    Touches lots only through `store` (DbLotStore live, MemLotStore for as-of reconstruction)."""
    t = txn.txn_type
    if t == InvTxnType.BUY:
        _create_lot(txn, store, _q_amount(txn.amount + txn.fee))  # commission capitalized
        return ZERO
    if t == InvTxnType.DIVIDEND_REINVEST:
        _create_lot(txn, store, _q_amount(txn.amount))
        return ZERO
    if t == InvTxnType.OPENING and txn.security_id:
        _create_lot(txn, store, _q_amount(txn.amount))
        return ZERO
    if t in (InvTxnType.SELL, InvTxnType.CASH_IN_LIEU):
        return _consume_lots(txn, store)  # cash-in-lieu = a fractional sell
    if t == InvTxnType.SELL_SHORT:
        # Open a short: a negative-quantity lot whose credit basis is the proceeds received.
        _make_lot(txn, store, txn.security, -_q_qty(txn.quantity), -_q_amount(txn.net_proceeds),
                  txn.date)
        return ZERO
    if t == InvTxnType.BUY_TO_COVER:
        return _consume_short(txn, store, _plan_short_draws(txn, store),
                              _q_amount(txn.amount + txn.fee))
    if t == InvTxnType.SPLIT:
        _apply_split(txn, store)
        return ZERO
    if t == InvTxnType.RETURN_OF_CAPITAL:
        return _apply_return_of_capital(txn, store)
    if t == InvTxnType.IN_KIND_OUT:
        return _apply_in_kind_out(txn, store)
    if t == InvTxnType.IN_KIND_IN:
        _apply_in_kind_in(txn, store)
        return ZERO
    if t == InvTxnType.WORTHLESS:
        return _apply_worthless(txn, store)
    if t == InvTxnType.CASH_MERGER:
        return _apply_cash_merger(txn, store)
    if t == InvTxnType.MERGER:
        _apply_merger(txn, store)
        return ZERO
    if t == InvTxnType.SPINOFF:
        return _apply_spinoff(txn, store)
    if t == InvTxnType.OPT_BUY_OPEN:
        _make_lot(txn, store, txn.security, txn.quantity, _q_amount(txn.amount + txn.fee), txn.date)
        return ZERO
    if t == InvTxnType.OPT_SELL_OPEN:
        # Write a short option: a negative-quantity lot with credit basis = premium received.
        _make_lot(txn, store, txn.security, -_q_qty(txn.quantity), -_q_amount(txn.net_proceeds),
                  txn.date)
        return ZERO
    if t == InvTxnType.OPT_SELL_CLOSE:
        return _consume_lots(txn, store)
    if t == InvTxnType.OPT_BUY_CLOSE:
        return _consume_short(txn, store, _plan_short_draws(txn, store),
                              _q_amount(txn.amount + txn.fee))
    if t == InvTxnType.OPT_EXPIRE:
        return _apply_option_expire(txn, store)
    if t == InvTxnType.OPT_EXERCISE:
        return _apply_exercise(txn, store)
    if t == InvTxnType.OPT_ASSIGN:
        return _apply_assign(txn, store)
    return ZERO


@dataclass
class RebuildResult:
    """Outcome of a register replay: which entries need re-posting downstream."""
    resell_ids: list[int]      # SELL / return-of-capital (etc.) whose realized gain shifted
    resync_out_ids: list[int]  # IN_KIND_OUT legs whose materialized lot_carry snapshot changed


# Types whose realized gain, if it shifts on replay, requires re-posting their GL entry.
_GAIN_TYPES = frozenset({
    InvTxnType.SELL, InvTxnType.CASH_IN_LIEU, InvTxnType.RETURN_OF_CAPITAL,
    InvTxnType.WORTHLESS, InvTxnType.CASH_MERGER, InvTxnType.SPINOFF,
    InvTxnType.BUY_TO_COVER,
    InvTxnType.OPT_SELL_CLOSE, InvTxnType.OPT_BUY_CLOSE, InvTxnType.OPT_EXPIRE,
    InvTxnType.OPT_EXERCISE, InvTxnType.OPT_ASSIGN,
})


def rebuild_account_lots(account) -> RebuildResult:
    """Wipe and replay the account's register in date order, rebuilding all lots + each
    disposition's realized gain + each in-kind-out's materialized snapshot. Returns the txns
    needing a re-post."""
    before = {t.id: (t.realized_gain, t.lot_carry) for t in account.transactions.all()}
    LotConsumption.objects.filter(sale_txn__account=account).delete()
    Lot.objects.filter(account=account).delete()

    store = DbLotStore(account)
    resell: list[int] = []
    resync: list[int] = []
    for txn in account.transactions.order_by("date", "id"):
        rg = _q_amount(_apply_lot_effect(txn, store))
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
    if t == InvTxnType.MARGIN_INTEREST:
        contra = txn.category_account or resolve_posting_account(
            acct, "margin_interest_expense", INTEREST_EXPENSE
        )
        return [line(contra, debit=amount, **payee), line(gl, credit=amount)]
    if t == InvTxnType.DIV_PAID_SHORT:
        contra = txn.category_account or resolve_posting_account(
            acct, "substitute_dividend_expense", SUBSTITUTE_DIVIDEND_EXPENSE
        )
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
    if t in (InvTxnType.SELL, InvTxnType.CASH_IN_LIEU, InvTxnType.RETURN_OF_CAPITAL,
             InvTxnType.WORTHLESS, InvTxnType.CASH_MERGER, InvTxnType.SPINOFF,
             InvTxnType.BUY_TO_COVER,
             InvTxnType.OPT_SELL_CLOSE, InvTxnType.OPT_BUY_CLOSE, InvTxnType.OPT_EXPIRE,
             InvTxnType.OPT_EXERCISE, InvTxnType.OPT_ASSIGN):
        # Only the realized gain/loss hits the ledger — the gl node already carries the position at
        # cost, so a disposition changes it by exactly (proceeds − cost). Cash-merger's cash comes
        # in via `signed_cash`; worthless has no cash (a pure basis write-off → capital loss);
        # buy-to-cover pays cash via `signed_cash`, realizing (short proceeds − buy-back cost). An
        # option exercise/assignment that ACQUIRES the underlying rolls basis in at zero gain → [].
        # A spin-off with cash-in-lieu realizes the gain on the fraction sold (else gain 0 → []).
        gain = _q_amount(txn.realized_gain)
        if gain > ZERO:
            return [line(gl, debit=gain), line(REALIZED_GAIN, credit=gain)]
        if gain < ZERO:
            g = -gain
            return [line(REALIZED_GAIN, debit=g), line(gl, credit=g)]
        return []
    if t in (InvTxnType.BUY, InvTxnType.SELL_SHORT,
             InvTxnType.SPLIT, InvTxnType.MERGER,
             InvTxnType.OPT_BUY_OPEN, InvTxnType.OPT_SELL_OPEN):
        # Cost-neutral: cash and total cost basis move equal-and-opposite, so nothing posts.
        # SELL_SHORT / OPT_SELL_OPEN mirror BUY — proceeds in via `signed_cash`, offset by a
        # credit-basis lot; OPT_BUY_OPEN mirrors BUY.
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


@transaction.atomic
def repool_security(security, *, user=None):
    """Re-run the lot engine for every account holding `security` after its `track_lots` setting was
    flipped, so existing lots collapse into (pooling on) or split back out of (pooling off) a single
    average-cost lot. Cash-neutral for a money-market fund (no gains to shift); reposts any
    disposition whose realized gain moved for a security that does have sells."""
    account_ids = list(
        Lot.objects.filter(security=security).values_list("account_id", flat=True).distinct()
    )
    for acct in InvestmentAccount.objects.filter(pk__in=account_ids):
        result = rebuild_account_lots(acct)
        _repost_shifted(result.resell_ids, user=user)
        _resync_out_legs(result.resync_out_ids, user=user)


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
    """For a cash transfer against a tracked counterparty, post the opposite leg so the 1150
    clearing account nets to zero across the two accounts — a banking leg when the other side is a
    bank account, or a mirror investment transfer when it is another of the household's investment
    accounts. Fire-and-forget (like a bank match): editing/deleting `txn` later won't re-sync it."""
    if txn.txn_type not in (InvTxnType.TRANSFER_IN, InvTxnType.TRANSFER_OUT):
        return None
    inv_type = (
        InvTxnType.TRANSFER_OUT
        if txn.txn_type == InvTxnType.TRANSFER_IN
        else InvTxnType.TRANSFER_IN
    )
    if txn.counter_investment_account_id is not None:
        # The other side is a tracked investment account: post the mirror transfer there so both
        # legs move cash↔1150 and the clearing account nets out.
        leg = InvestmentTransaction.objects.create(
            account=txn.counter_investment_account,
            txn_type=inv_type,
            date=txn.date,
            amount=txn.amount,
            counter_investment_account=txn.account,
            memo=txn.memo,
            reference=txn.reference,
        )
        apply_transaction(leg, user=user, is_new=True)
        return leg
    if txn.counter_account_id is None:
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
    # select_related the security so signed_cash can read an option's right without an N+1.
    txns = account.transactions.select_related("security")
    return _q_amount(sum((t.signed_cash for t in txns), ZERO))


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
        # abs() keeps the sign meaningful for shorts/written options (negative credit basis).
        base = abs(self.cost_basis)
        return _q_amount(self.unrealized_gain / base * 100) if base else ZERO


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
        cost = _q_amount(r["cost"])
        if qty == ZERO and cost == ZERO:
            continue  # fully flat (e.g. a fully-covered short) — drop; shorts show as negatives
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
            if h.security.asset_class == AssetClass.DERIVATIVE:
                continue  # options never render as a donut arc (may be net-negative when written)
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
    txns = account.transactions.select_related(
        "security", "security__underlying", "counter_account", "counter_investment_account",
        "target_security", "payee_person", "payee_organization",
    ).order_by("date", "id")
    for txn in txns:
        running = _q_amount(running + txn.signed_cash)
        rows.append({"txn": txn, "balance": running})
    rows.reverse()
    return rows


def contribution_summary(account) -> list[dict]:
    """Per-tax-year contribution totals for a year-tracked account (IRA/HSA/529): the sum of
    contributions + transfers-in tagged with a `tax_year`, newest year first. Pure module rollup —
    reads the attribution metadata, touches no GL. Empty for accounts with no tracked year."""
    from apps.investments.models import CONTRIBUTION_TAX_YEAR_TYPES

    rows = (
        account.transactions
        .filter(tax_year__isnull=False, txn_type__in=CONTRIBUTION_TAX_YEAR_TYPES)
        .values("tax_year")
        .annotate(total=Sum("amount"))
        .order_by("-tax_year")
    )
    return [{"year": r["tax_year"], "total": _q_amount(r["total"] or ZERO)} for r in rows]


def institution_summary() -> list[dict]:
    """Per-brokerage rollup for the Institutions index: every Brokerage-tagged organization with its
    investment accounts and their combined totals (value / market / cash / cost / unrealized), most
    valuable first. Brokerages with no accounts yet are included (zero totals) so a freshly-added
    one still appears and can receive accounts. Pure read — no GL involvement."""
    from apps.organizations.models import Organization

    orgs = list(
        Organization.objects.filter(categories__kind="ORG", categories__name="Brokerage")
        .distinct().order_by("name")
    )
    by_org: dict[int, list] = {}
    for acct in InvestmentAccount.objects.select_related("institution", "currency"):
        by_org.setdefault(acct.institution_id, []).append(acct)

    rows = [institution_row(org, by_org.get(org.id, [])) for org in orgs]
    rows.sort(key=lambda r: r["total_value"], reverse=True)
    return rows


def institution_row(org, accounts=None) -> dict:
    """Totals for one brokerage over the given accounts (defaults to its investment accounts)."""
    if accounts is None:
        accounts = list(org.investment_accounts.select_related("currency"))
    market = _q_amount(sum((a.market_value for a in accounts), ZERO))
    cash = _q_amount(sum((a.cash_balance for a in accounts), ZERO))
    cost = _q_amount(sum((a.cost_basis for a in accounts), ZERO))
    return {
        "org": org,
        "accounts": accounts,
        "account_count": len(accounts),
        "market": market,
        "cash": cash,
        "cost": cost,
        "total_value": _q_amount(market + cash),
        "unrealized": _q_amount(market - cost),
    }


# --- Per-instrument performance report -------------------------------------------------------
#
# Which transaction types feed each performance metric. Acquisitions add to "bought"; sells /
# in-kind-out add to "sold"; sells + cash-buyouts add to "amount sold"; dividends (incl. reinvested)
# and cap-gain distributions are income, as is interest (kept separate). Fees + realized gain are
# summed off every txn's own fields.
_PERF_ACQUIRE = frozenset({
    InvTxnType.BUY, InvTxnType.DIVIDEND_REINVEST, InvTxnType.OPENING, InvTxnType.IN_KIND_IN,
})
_PERF_DISPOSE_QTY = frozenset({InvTxnType.SELL, InvTxnType.CASH_IN_LIEU, InvTxnType.IN_KIND_OUT})
_PERF_SOLD_AMOUNT = frozenset({InvTxnType.SELL, InvTxnType.CASH_IN_LIEU, InvTxnType.CASH_MERGER})
_PERF_DIVIDEND = frozenset({
    InvTxnType.DIVIDEND, InvTxnType.DIVIDEND_REINVEST, InvTxnType.CAP_GAIN_DIST,
})


@dataclass
class PerfRow:
    """One instrument's performance in an account: lifetime quantities/income/fees + the current
    position's cost, price and gain. `gain` = realized + unrealized (capital only); `total_return`
    also folds in income (dividends + interest). `invested` is the total cost basis put in (held +
    already disposed); `return_pct` = total_return / invested as a %, so held or fully-sold
    positions rank best-to-worst; None when nothing was invested (income only)."""
    security: object
    qty_bought: Decimal
    qty_sold: Decimal
    current_qty: Decimal
    cost_basis: Decimal
    fees: Decimal
    dividends: Decimal
    interest: Decimal
    amount_sold: Decimal
    realized: Decimal
    price: object
    market_value: Decimal
    unrealized: Decimal
    gain: Decimal
    income: Decimal
    total_return: Decimal
    invested: Decimal
    return_pct: object


_PERF_MONEY_TOTALS = (
    "cost_basis", "fees", "dividends", "interest", "amount_sold",
    "realized", "market_value", "gain", "income", "total_return", "invested",
)


def _return_pct(total_return, invested):
    """Total return as a percentage of the cost basis invested, or None when nothing was invested
    (a pure income row) — a return on $0 is undefined rather than infinite."""
    if invested is None or invested <= ZERO:
        return None
    return (total_return / invested * Decimal("100")).quantize(Decimal("0.01"))


def security_performance(account) -> dict:
    """Per-instrument performance rows for an account + a money-column totals footer. Includes
    fully-sold instruments (realized gain / amount sold still show), not just current holdings.
    Read-only — aggregates the register and the open lots; no GL involvement."""
    agg: dict[int, dict] = {}

    def bucket(security):
        b = agg.get(security.id)
        if b is None:
            b = agg[security.id] = {
                "security": security, "qty_bought": ZERO, "qty_sold": ZERO, "current_qty": ZERO,
                "cost_basis": ZERO, "fees": ZERO, "dividends": ZERO, "interest": ZERO,
                "amount_sold": ZERO, "realized": ZERO,
            }
        return b

    for t in account.transactions.filter(security__isnull=False).select_related("security"):
        b = bucket(t.security)
        b["fees"] += t.fee
        b["realized"] += t.realized_gain
        tt = t.txn_type
        if tt in _PERF_ACQUIRE:
            b["qty_bought"] += t.quantity
        if tt in _PERF_DISPOSE_QTY:
            b["qty_sold"] += t.quantity
        if tt in _PERF_SOLD_AMOUNT:
            b["amount_sold"] += t.amount
        if tt in _PERF_DIVIDEND:
            b["dividends"] += t.amount
        elif tt == InvTxnType.INTEREST:
            b["interest"] += t.amount

    for lot in account.lots.filter(open=True).select_related("security"):
        b = bucket(lot.security)
        b["current_qty"] += lot.remaining_quantity
        b["cost_basis"] += lot.cost_basis

    rows: list[PerfRow] = []
    totals = dict.fromkeys(_PERF_MONEY_TOTALS, ZERO)
    for b in agg.values():
        sec = b["security"]
        price = sec.latest_price
        qty = _q_qty(b["current_qty"])
        cost = _q_amount(b["cost_basis"])
        mv = _q_amount(qty * price) if price is not None else cost
        realized = _q_amount(b["realized"])
        amount_sold = _q_amount(b["amount_sold"])
        unrealized = _q_amount(mv - cost)
        gain = _q_amount(realized + unrealized)
        income = _q_amount(b["dividends"] + b["interest"])
        total_return = _q_amount(gain + income)
        # Cost basis put in = still held (cost) + already disposed (proceeds − realized gain).
        invested = _q_amount((amount_sold - realized) + cost)
        row = PerfRow(
            security=sec, qty_bought=_q_qty(b["qty_bought"]), qty_sold=_q_qty(b["qty_sold"]),
            current_qty=qty, cost_basis=cost, fees=_q_amount(b["fees"]),
            dividends=_q_amount(b["dividends"]), interest=_q_amount(b["interest"]),
            amount_sold=amount_sold, realized=realized, price=price,
            market_value=mv, unrealized=unrealized, gain=gain, income=income,
            total_return=total_return, invested=invested,
            return_pct=_return_pct(total_return, invested),
        )
        rows.append(row)
        for k in _PERF_MONEY_TOTALS:
            totals[k] += getattr(row, k)
    # Best performer first (by % return), rows with no invested basis (income only) last.
    rows.sort(key=lambda r: (r.return_pct is not None, r.return_pct or ZERO), reverse=True)
    money_totals = {k: _q_amount(v) for k, v in totals.items()}
    money_totals["return_pct"] = _return_pct(money_totals["total_return"], money_totals["invested"])
    return {"rows": rows, "totals": money_totals}


def income_summary(account) -> dict:
    """Income collected in an account — dividends (incl. reinvested) + interest + capital-gain
    distributions — grouped by the year received (transaction date), newest first, plus the lifetime
    total. Read-only rollup; no GL involvement."""
    from apps.investments.models import INCOME_TXN_TYPES

    rows = (
        account.transactions
        .filter(txn_type__in=INCOME_TXN_TYPES)
        .annotate(yr=ExtractYear("date"))
        .values("yr").annotate(total=Sum("amount")).order_by("-yr")
    )
    by_year = [{"year": r["yr"], "total": _q_amount(r["total"] or ZERO)} for r in rows]
    return {
        "by_year": by_year,
        "total": _q_amount(sum((r["total"] for r in by_year), ZERO)),
        "has_income": bool(by_year),
    }


def transfer_totals(account) -> dict:
    """Total cash moved into / out of the account via transfers (TRANSFER_IN / TRANSFER_OUT),
    whether the other side is a bank or another investment account. Read-only rollup; no GL."""
    agg = (
        account.transactions
        .filter(txn_type__in=[InvTxnType.TRANSFER_IN, InvTxnType.TRANSFER_OUT])
        .values("txn_type").annotate(total=Sum("amount"))
    )
    by_type = {r["txn_type"]: _q_amount(r["total"] or ZERO) for r in agg}
    return {
        "transfer_in": by_type.get(InvTxnType.TRANSFER_IN, ZERO),
        "transfer_out": by_type.get(InvTxnType.TRANSFER_OUT, ZERO),
    }


def contribution_limit_status(account, as_of=None):
    """Per-tax-year progress against the shared annual IRS limit for an IRA/HSA account. `used` is
    aggregated across the primary holder's accounts in the same category (a person's IRAs share ONE
    cap; HSAs are per person), catch-up is applied from the holder's birth year (50+ IRA / 55+ HSA),
    and the HSA limit follows this account's self/family coverage. Returns None for accounts with no
    simple annual limit (SEP/SIMPLE/529/taxable) — the plain by-year rollup covers those. Pure read
    — attribution metadata only, no GL."""
    from apps.investments.models import (
        CONTRIBUTION_TAX_YEAR_TYPES,
        HSA_CATCHUP_AGE,
        IRA_CATCHUP_AGE,
        IRA_LIMIT_REGISTRATIONS,
        ContributionLimit,
        HsaCoverage,
        Registration,
    )

    category = account.contribution_limit_category
    if category is None:
        return None
    as_of = as_of or datetime.date.today()

    # Whose limit is it, and which accounts share it? (An IRA/HSA is single-owner.)
    holder = account.holders.filter(is_primary=True).first() or account.holders.first()
    person = holder.person if holder else None
    regs = IRA_LIMIT_REGISTRATIONS if category == "ira" else frozenset({Registration.HSA})
    shared = InvestmentAccount.objects.filter(registration__in=regs)
    shared = shared.filter(holders__person=person).distinct() if person else shared.filter(
        pk=account.pk)
    acct_ids = list(shared.values_list("pk", flat=True))

    agg = (
        InvestmentTransaction.objects
        .filter(account_id__in=acct_ids, tax_year__isnull=False,
                txn_type__in=CONTRIBUTION_TAX_YEAR_TYPES)
        .values("tax_year").annotate(total=Sum("amount"))
    )
    used_by_year = {r["tax_year"]: _q_amount(r["total"] or ZERO) for r in agg}
    this_by_year = {r["year"]: r["total"] for r in contribution_summary(account)}
    catch_age = IRA_CATCHUP_AGE if category == "ira" else HSA_CATCHUP_AGE
    # Editable IRS figures from Setup; a year without a row shows no bar (graceful).
    limits_by_year = {cl.tax_year: cl for cl in ContributionLimit.objects.all()}

    rows = []
    for year in sorted(set(used_by_year) | {as_of.year}, reverse=True):
        used = used_by_year.get(year, ZERO)
        limits = limits_by_year.get(year)
        if limits is None:  # year not configured in Setup — show the total with no bar
            rows.append({"year": year, "used": used, "limit": None,
                         "this_account": _q_amount(this_by_year.get(year, ZERO))})
            continue
        if category == "ira":
            base, catchup = limits.ira, limits.ira_catchup
        else:
            base = (limits.hsa_family if account.hsa_coverage == HsaCoverage.FAMILY
                    else limits.hsa_self)
            catchup = limits.hsa_catchup
        eligible = bool(person and person.dob_year and (year - person.dob_year) >= catch_age)
        limit = base + (catchup if eligible else ZERO)
        pct = int(min(used / limit * 100, 100)) if limit else 0
        rows.append({
            "year": year, "used": used, "limit": limit,
            "catch_up": catchup if eligible else ZERO,
            "remaining": _q_amount(limit - used) if used < limit else ZERO,
            "over_by": _q_amount(used - limit) if used > limit else ZERO,
            "pct": pct, "over": used > limit,
            "this_account": _q_amount(this_by_year.get(year, ZERO)),
        })

    return {
        "category": category,
        "category_label": "IRA" if category == "ira" else "HSA",
        "person": person,
        "coverage_label": account.get_hsa_coverage_display() if category == "hsa" else "",
        "account_count": len(acct_ids),
        "rows": rows,
    }


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


# --- Vesting overlay (module-level view; never touches the GL) --------------------------------

def grant_row(grant, as_of=None) -> dict:
    """A grant's vested/unvested split, $ values, and next vest event, as of a date."""
    return {
        "grant": grant,
        "fraction": grant.vested_fraction(as_of),
        "vested": grant.vested(as_of),
        "unvested": grant.unvested(as_of),
        "vested_value": grant.vested_value(as_of),
        "unvested_value": grant.unvested_value(as_of),
        "next": grant.next_vest(as_of),
    }


def vesting_summary(account, as_of=None):
    """(rows, totals) for an account's vesting grants. `at_risk` = unvested $ of FUNDED grants
    (present but forfeitable → reduces the module's vested value); `upcoming` = unvested $ of
    UNFUNDED grants (future inflows, not in the balance yet); `vested_value` = account total value
    − at_risk. Pure read — no GL involvement."""
    grants = list(account.vesting_grants.prefetch_related("tranches"))
    rows = [grant_row(g, as_of) for g in grants]
    at_risk = _q_amount(sum((r["unvested_value"] for r in rows if r["grant"].funded), ZERO))
    upcoming = _q_amount(sum((r["unvested_value"] for r in rows if not r["grant"].funded), ZERO))
    return rows, {
        "at_risk": at_risk,
        "upcoming": upcoming,
        "vested_value": _q_amount(account.total_value - at_risk),
        "has_grants": bool(rows),
    }


def unvested_at_risk_total(as_of=None) -> Decimal:
    """Portfolio-wide unvested-but-present (forfeitable) value — the sum over FUNDED grants."""
    total = ZERO
    for g in VestingGrant.objects.filter(funded=True).prefetch_related("tranches"):
        total += g.unvested_value(as_of)
    return _q_amount(total)


def upcoming_vesting(within_days: int = 365, as_of=None) -> list[dict]:
    """UNFUNDED grants (e.g. RSUs) whose next vest event falls within the window, soonest first —
    the household's upcoming equity/match vesting feed."""
    as_of = as_of or datetime.date.today()
    horizon = as_of + datetime.timedelta(days=within_days)
    out = []
    for g in VestingGrant.objects.filter(funded=False).prefetch_related("tranches"):
        nxt = g.next_vest(as_of)
        if nxt is not None and nxt.vest_date <= horizon:
            out.append({"grant": g, "next": nxt, "unvested_value": g.unvested_value(as_of)})
    out.sort(key=lambda r: r["next"].vest_date)
    return out


# --- Value over time (module overlay — never touches the GL) --------------------------------
#
# A computed, retroactive time series of portfolio value. The INVESTED/cost line comes free & exact
# from the GL (`account_balance(gl, as_of=T)`); the MARKET line needs per-security holdings as of a
# past date, reconstructed by replaying the register against a non-mutating MemLotStore (the SAME
# engine that produces the live lots — no parallel implementation to drift).

def price_as_of(security, on_date) -> Decimal | None:
    """The carry-forward mark: the latest manually-entered price on/before `on_date`, else None.
    Mirrors finance.rate_to_base. For one-off use; batch series use `PriceCarry`."""
    return (
        SecurityPrice.objects
        .filter(security_id=_sid(security), as_of__lte=on_date)
        .values_list("price", flat=True)
        .first()  # SecurityPrice Meta ordering is -as_of → latest on/before
    )


class PriceCarry:
    """Batch carry-forward pricing loaded in a single query: `price_at(security_id, date)` returns
    the latest price on/before `date` (else None) via bisect over each security's sorted history."""

    def __init__(self, security_ids, *, up_to=None):
        self._by_sec: dict[int, tuple[list, list]] = {}
        qs = SecurityPrice.objects.filter(security_id__in=list(security_ids))
        if up_to is not None:
            qs = qs.filter(as_of__lte=up_to)
        for sid, as_of, price in qs.order_by("security_id", "as_of").values_list(
            "security_id", "as_of", "price"
        ):
            dates, prices = self._by_sec.setdefault(sid, ([], []))
            dates.append(as_of)
            prices.append(price)

    def price_at(self, security_id, on_date) -> Decimal | None:
        entry = self._by_sec.get(security_id)
        if not entry:
            return None
        dates, prices = entry
        i = bisect.bisect_right(dates, on_date)
        return prices[i - 1] if i else None


def position_snapshots(account, dates) -> dict:
    """Reconstruct `{date: (cash, {security_id: (qty, cost)})}` for `account` at each date, via a
    non-mutating MemLotStore replay of the register. `cash` = Σ signed_cash over txns with date ≤ d;
    positions = the open-lot aggregate at d. One ascending pass covers every date. Never writes."""
    dates = sorted(dates)
    store = MemLotStore()
    txns = list(
        account.transactions.select_related(
            "security", "security__underlying", "target_security"
        ).order_by("date", "id")
    )
    out: dict = {}
    cash = ZERO
    ti = 0
    n = len(txns)
    for d in dates:
        while ti < n and txns[ti].date <= d:
            txn = txns[ti]
            cash = _q_amount(cash + txn.signed_cash)
            _apply_lot_effect(txn, store)
            ti += 1
        out[d] = (cash, store.open_positions())
    return out


def positions_as_of(account, on_date) -> tuple:
    """(cash, {security_id: (qty, cost)}) for `account` as of `on_date` — a single-date snapshot."""
    return position_snapshots(account, [on_date])[on_date]


_RANGE_DAYS = {"3M": 90, "1Y": 365}


def value_over_time(range_key: str = "1Y", *, accounts=None, today=None) -> dict:
    """Portfolio value time series over a range (3M / 1Y / ALL), summed across `accounts`. Returns
    two lines evaluated at each event date (txn or price change): INVESTED = cash + Σ open-lot cost,
    MARKET = cash + Σ (qty × carry-forward price, or cost when unpriced). Both share the same cash,
    so their gap is unrealized gain. Base==native currency assumed (as elsewhere in the module).

    Purely computed and read-only — reconstructs holdings via the MemLotStore replay; posts nothing.
    """
    today = today or datetime.date.today()
    range_key = range_key if range_key in ("3M", "1Y", "ALL") else "1Y"
    if accounts is None:
        accounts = list(InvestmentAccount.objects.all())

    # Held securities (for pricing) + earliest activity (for the ALL range), one pass per account.
    held: set[int] = set()
    earliest = None
    for acct in accounts:
        for d, sid in acct.transactions.values_list("date", "security_id"):
            if sid:
                held.add(sid)
            earliest = d if earliest is None or d < earliest else earliest

    if earliest is None:  # empty portfolio — a single flat point at today
        return {
            "range": range_key, "start": today, "end": today,
            "series": [(today, ZERO, ZERO)], "min": ZERO, "max": ZERO,
            "last_invested": ZERO, "last_market": ZERO, "gain": ZERO,
        }

    if range_key == "ALL":
        start = earliest
    else:
        start = today - datetime.timedelta(days=_RANGE_DAYS[range_key])

    # Event dates = txn dates + price-change dates within [start, today], + start anchor + today.
    dates: set[datetime.date] = {start, today}
    for acct in accounts:
        for d in acct.transactions.filter(
            date__gte=start, date__lte=today
        ).values_list("date", flat=True):
            dates.add(d)
    if held:
        for d in SecurityPrice.objects.filter(
            security_id__in=held, as_of__gte=start, as_of__lte=today
        ).values_list("as_of", flat=True):
            dates.add(d)
    event_dates = sorted(d for d in dates if start <= d <= today)

    carry = PriceCarry(held, up_to=today)
    per_account = [position_snapshots(acct, event_dates) for acct in accounts]

    series: list[tuple] = []
    for d in event_dates:
        cash = ZERO
        cost_total = ZERO
        market_total = ZERO
        for snaps in per_account:
            c, positions = snaps[d]
            cash = _q_amount(cash + c)
            for sid, (qty, cost_s) in positions.items():
                cost_total = _q_amount(cost_total + cost_s)
                price = carry.price_at(sid, d)
                mv = _q_amount(qty * price) if price is not None else cost_s
                market_total = _q_amount(market_total + mv)
        series.append((d, _q_amount(cash + cost_total), _q_amount(cash + market_total)))

    vals = [v for _, inv, mkt in series for v in (inv, mkt)]
    last = series[-1]
    return {
        "range": range_key, "start": start, "end": today, "series": series,
        "min": min(vals), "max": max(vals),
        "last_invested": last[1], "last_market": last[2],
        "gain": _q_amount(last[2] - last[1]),
    }


# Chart geometry (fixed viewBox; templates can't do arithmetic — precompute here, like the donut).
CHART_W = 640
CHART_H = 200
CHART_PAD_X = 8
CHART_PAD_TOP = 12
CHART_PAD_BOT = 22


def line_chart_points(series, *, min_v, max_v, start, end,
                      width: int = CHART_W, height: int = CHART_H) -> dict:
    """Precompute SVG geometry for a two-line value chart: `invested_points`/`market_points` for
    <polyline>, a `gain_area_d` <path> filling the band between the lines, per-point dots, and axis
    ticks. Coordinates are pre-formatted (templates do no math). Guards flat/single-point series."""
    plot_w = width - CHART_PAD_X * 2
    plot_h = height - CHART_PAD_TOP - CHART_PAD_BOT
    span = max_v - min_v
    pad = max(Decimal("1"), _q_amount(span * Decimal("0.08")))
    y_lo, y_hi = min_v - pad, max_v + pad
    if y_hi <= y_lo:  # fully flat — give the axis a unit of breathing room
        y_lo, y_hi = min_v - Decimal("1"), min_v + Decimal("1")
    total_days = (end - start).days or 1
    y_range = float(y_hi - y_lo)

    def px(d) -> float:
        return CHART_PAD_X + float((d - start).days) / total_days * plot_w

    def py(v) -> float:
        return CHART_PAD_TOP + (float(y_hi) - float(v)) / y_range * plot_h

    inv_coords = [(px(d), py(inv)) for d, inv, _mkt in series]
    mkt_coords = [(px(d), py(mkt)) for d, _inv, mkt in series]
    invested_points = " ".join(f"{x:.2f},{y:.2f}" for x, y in inv_coords)
    market_points = " ".join(f"{x:.2f},{y:.2f}" for x, y in mkt_coords)

    # Gain band: along market forward, back along invested, closed.
    band = mkt_coords + list(reversed(inv_coords))
    if band:
        gain_area_d = "M " + " L ".join(f"{x:.2f},{y:.2f}" for x, y in band) + " Z"
    else:
        gain_area_d = ""

    points = [
        {"date": d, "invested": inv, "market": mkt,
         "x": round(px(d), 2), "y_inv": round(py(inv), 2), "y_mkt": round(py(mkt), 2)}
        for d, inv, mkt in series
    ]
    mid = start + datetime.timedelta(days=total_days // 2)
    y_mid = _q_amount((y_hi + y_lo) / 2)
    return {
        "invested_points": invested_points,
        "market_points": market_points,
        "gain_area_d": gain_area_d,
        "points": points,
        "y_ticks": [{"value": v, "y": round(py(v), 2)} for v in (y_hi, y_mid, y_lo)],
        "x_ticks": [{"date": dt, "x": round(px(dt), 2)} for dt in (start, mid, end)],
        "width": width, "height": height, "view_box": f"0 0 {width} {height}",
    }
