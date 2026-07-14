"""Amortization & payoff projection — pure functions, no DB, posts nothing.

This is the planning overlay for installment loans (the hybrid-amortization decision): it produces a
schedule for display, pre-fills a payment's principal/interest split, and projects the remaining
payoff — but the general ledger is the source of truth. `suggest_split` and `payoff_projection`
always re-derive from the CURRENT balance, so any user override or prepayment self-corrects.

All rates are DECIMAL FRACTIONS (0.065 == 6.5%); callers convert a stored percent APR via apr/100.
Money is quantized ROUND_HALF_UP to `places` (default 2; pass `currency.decimal_places` for JPY).
Frequency is parameterized: `period_rate = annual_rate / periods_per_year`, and dates step per the
frequency. `payoff_projection` is the only function that reads the GL (via `account_balance`); it
never writes.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from apps.core.dates import _clamp_day

ZERO = Decimal("0")

# Periods per year by frequency string (the values of loans.models.Frequency). Kept here so the
# engine stays import-light; a test asserts it matches loans.models.PERIODS_PER_YEAR.
PERIODS_PER_YEAR = {
    "monthly": 12,
    "semi_monthly": 24,
    "bi_weekly": 26,
    "weekly": 52,
}
# Loop backstop: a well-formed loan pays off within its term (extra only shortens it); this caps a
# pathological too-low payment instead of looping forever.
_MAX_PERIODS = 2000


@dataclass(frozen=True)
class SchedulePeriod:
    n: int
    date: datetime.date
    payment: Decimal
    interest: Decimal
    principal: Decimal        # scheduled principal portion
    extra_principal: Decimal  # additional principal applied this period
    balance: Decimal          # remaining balance after this payment


def _q(places: int) -> Decimal:
    return Decimal(1).scaleb(-places)


def _round(amount, places: int) -> Decimal:
    return Decimal(amount).quantize(_q(places), rounding=ROUND_HALF_UP)


def add_months(d: datetime.date, n: int) -> datetime.date:
    """`d` shifted by `n` calendar months, clamping the day to the month (Jan-31 +1 → Feb-28).
    The monthly analog of core/dates.next_occurrence (which is yearly-only)."""
    total = (d.month - 1) + n
    year = d.year + total // 12
    month = total % 12 + 1
    return datetime.date(year, month, _clamp_day(year, month, d.day))


def periods_per_year(frequency: str) -> int:
    return PERIODS_PER_YEAR.get(frequency, 12)


def period_count(term_months: int, frequency: str) -> int:
    """Number of payments over the term at the given frequency (monthly 12mo → 12; weekly → 52)."""
    return int(round(term_months * periods_per_year(frequency) / 12))


def schedule_date(anchor: datetime.date, i: int, frequency: str, payment_day=None) -> datetime.date:
    """The date of period `i` (0-indexed), counting from the first-payment `anchor`."""
    if frequency == "weekly":
        return anchor + datetime.timedelta(days=7 * i)
    if frequency == "bi_weekly":
        return anchor + datetime.timedelta(days=14 * i)
    if frequency == "semi_monthly":
        base = add_months(anchor, i // 2)
        first = payment_day or anchor.day
        day = first if i % 2 == 0 else first + 15
        return datetime.date(base.year, base.month, _clamp_day(base.year, base.month, day))
    # monthly
    base = add_months(anchor, i)
    if payment_day:
        return datetime.date(base.year, base.month, _clamp_day(base.year, base.month, payment_day))
    return base


def next_payment_date(prev: datetime.date, frequency: str, payment_day=None) -> datetime.date:
    """One period after `prev` — the anchor for a forward projection from today."""
    if frequency == "weekly":
        return prev + datetime.timedelta(days=7)
    if frequency == "bi_weekly":
        return prev + datetime.timedelta(days=14)
    if frequency == "semi_monthly":
        return prev + datetime.timedelta(days=15)
    nd = add_months(prev, 1)
    if payment_day:
        return datetime.date(nd.year, nd.month, _clamp_day(nd.year, nd.month, payment_day))
    return nd


def level_payment(principal, period_rate, n: int, places: int = 2) -> Decimal:
    """The level payment A that amortizes `principal` over `n` periods at `period_rate`:
    A = P·r / (1 − (1+r)^−n); r == 0 → P/n."""
    principal = Decimal(principal)
    period_rate = Decimal(period_rate)
    if n <= 0:
        return _round(principal, places)
    if period_rate == ZERO:
        return _round(principal / n, places)
    factor = (Decimal(1) + period_rate) ** n
    payment = principal * period_rate * factor / (factor - Decimal(1))
    return _round(payment, places)


def amortization_schedule(
    principal,
    annual_rate,
    term_months: int,
    start_date: datetime.date,
    *,
    frequency: str = "monthly",
    first_payment_date: datetime.date | None = None,
    payment_amount=None,
    payment_day=None,
    extra_principal=ZERO,
    places: int = 2,
) -> list[SchedulePeriod]:
    """A full amortization schedule. `annual_rate` is a fraction; `payment_amount` defaults to the
    level payment. `extra_principal` is applied every period (shortens the payoff). The final period
    carries the residual so Σ principal (+ extra) == `principal` exactly. Raises ValueError on
    negative amortization (a payment that never reduces the balance)."""
    principal = _round(principal, places)
    if principal <= ZERO or not term_months:
        return []
    ppy = periods_per_year(frequency)
    n = period_count(term_months, frequency)
    period_rate = (Decimal(annual_rate) / ppy) if annual_rate else ZERO
    pay = (
        _round(payment_amount, places)
        if payment_amount is not None
        else level_payment(principal, period_rate, n, places)
    )
    extra = _round(extra_principal or ZERO, places)
    anchor = first_payment_date or next_payment_date(start_date, frequency, payment_day)

    periods: list[SchedulePeriod] = []
    balance = principal
    i = 0
    while balance > ZERO and i < _MAX_PERIODS:
        interest = _round(balance * period_rate, places)
        scheduled_principal = pay - interest
        if scheduled_principal < ZERO:
            scheduled_principal = ZERO
        reduction = scheduled_principal + extra
        if reduction <= ZERO:
            raise ValueError("Payment does not cover interest (negative amortization).")
        # The scheduled last period (i == n-1) trues up any rounding residual (or balloons a
        # too-low payment) so a fully-amortizing loan lands on exactly n periods and Σ principal
        # matches. Extra principal can hit zero earlier, ending the loop before then.
        is_final = reduction >= balance or i == n - 1
        if is_final:
            applied_extra = extra if extra <= balance else balance
            scheduled_principal = balance - applied_extra
            payment_this = interest + balance
            balance = ZERO
        else:
            applied_extra = extra
            payment_this = interest + scheduled_principal + applied_extra
            balance = balance - reduction
        periods.append(
            SchedulePeriod(
                n=i + 1,
                date=schedule_date(anchor, i, frequency, payment_day),
                payment=payment_this,
                interest=interest,
                principal=scheduled_principal,
                extra_principal=applied_extra,
                balance=balance,
            )
        )
        i += 1
    return periods


def suggest_split(current_balance, annual_rate, payment_amount, *, frequency="monthly", places=2):
    """The hybrid pre-fill for recording a payment: interest = balance × period_rate (the interest
    actually due), principal = payment − interest, clamped to [0, balance]. Returns {interest,
    principal}. A payment below the interest due books all-interest; an overpayment caps principal
    at the balance (the surplus is the caller's to handle)."""
    balance = _round(current_balance, places)
    payment = _round(payment_amount, places)
    ppy = periods_per_year(frequency)
    period_rate = (Decimal(annual_rate) / ppy) if annual_rate else ZERO
    due = _round(balance * period_rate, places)
    if due < ZERO:
        due = ZERO
    interest = due if due <= payment else payment
    principal = payment - interest
    if principal < ZERO:
        principal = ZERO
    if principal > balance:
        principal = balance
    return {"interest": interest, "principal": principal}


def payoff_projection(loan, *, as_of=None, extra_principal=ZERO) -> dict:
    """Project a loan forward from its CURRENT ledger balance (a pure read — never posts).

    Installment loans get a fresh schedule from the current balance at the current rate → payoff
    date, remaining interest, and a balance series for the paydown chart. Revolving / "other" loans
    have no fixed schedule (returns available_credit / utilization instead)."""
    from apps.finance.services import account_balance

    today = as_of or datetime.date.today()
    balance = account_balance(loan.gl_account) if loan.gl_account_id else ZERO
    places = loan.currency.decimal_places

    if loan.is_revolving or not loan.is_installment:
        return {
            "schedule": None,
            "periods": [],
            "payoff_date": None,
            "remaining_interest": ZERO,
            "balance_series": [(today, balance)],
            "available_credit": loan.available_credit,
            "utilization": loan.utilization,
        }

    if balance <= ZERO or not loan.payment_amount or not loan.term_months:
        return {
            "schedule": [],
            "periods": [],
            "payoff_date": None,
            "remaining_interest": ZERO,
            "balance_series": [(today, balance)],
            "available_credit": None,
            "utilization": None,
        }

    rate = (loan.current_rate or ZERO) / Decimal(100)
    periods = amortization_schedule(
        balance,
        rate,
        loan.term_months,
        today,
        frequency=loan.payment_frequency,
        payment_amount=loan.payment_amount,
        payment_day=loan.payment_day,
        extra_principal=extra_principal,
        places=places,
    )
    remaining_interest = sum((p.interest for p in periods), ZERO)
    balance_series = [(today, balance)] + [(p.date, p.balance) for p in periods]
    return {
        "schedule": periods,
        "periods": periods,
        "payoff_date": periods[-1].date if periods else None,
        "remaining_interest": remaining_interest,
        "balance_series": balance_series,
        "available_credit": None,
        "utilization": None,
    }
