"""Amortization engine — pure Decimal math, no DB. Covers the payment formula, exact principal
conservation, the final residual, zero-rate / per-frequency schedules, the hybrid split pre-fill,
date arithmetic, and the what-if extra-principal delta."""

import datetime
from decimal import Decimal

import pytest

from apps.loans.amortization import (
    PERIODS_PER_YEAR,
    add_months,
    amortization_schedule,
    level_payment,
    period_count,
    suggest_split,
)

D = Decimal


def _sum_principal(schedule):
    return sum((p.principal + p.extra_principal for p in schedule), D("0"))


def test_periods_per_year_matches_model():
    from apps.loans.models import PERIODS_PER_YEAR as MODEL_PPY

    assert {str(k): v for k, v in MODEL_PPY.items()} == PERIODS_PER_YEAR


def test_level_payment_classic_30yr_mortgage():
    # $100,000 @ 6% APR over 360 monthly payments → the well-known $599.55.
    assert level_payment(D("100000"), D("0.06") / 12, 360) == D("599.55")


def test_level_payment_zero_rate_is_even_split():
    assert level_payment(D("12000"), D("0"), 12) == D("1000.00")


def test_schedule_conserves_principal_and_ends_at_zero():
    sched = amortization_schedule(
        D("100000"), D("0.06"), 360, datetime.date(2026, 1, 1), frequency="monthly"
    )
    assert len(sched) == 360
    assert _sum_principal(sched) == D("100000.00")  # exact — final residual absorbs rounding
    assert sched[-1].balance == D("0")
    # Interest is highest in period 1 and declines.
    assert sched[0].interest > sched[-1].interest


def test_zero_rate_schedule_has_no_interest():
    sched = amortization_schedule(
        D("12000"), D("0"), 12, datetime.date(2026, 1, 1), frequency="monthly"
    )
    assert len(sched) == 12
    assert all(p.interest == D("0") for p in sched)
    assert _sum_principal(sched) == D("12000.00")


@pytest.mark.parametrize(
    "frequency,expected_n",
    [("monthly", 12), ("semi_monthly", 24), ("bi_weekly", 26), ("weekly", 52)],
)
def test_schedule_per_frequency(frequency, expected_n):
    sched = amortization_schedule(
        D("12000"), D("0"), 12, datetime.date(2026, 1, 1), frequency=frequency
    )
    assert period_count(12, frequency) == expected_n
    assert len(sched) == expected_n
    assert _sum_principal(sched) == D("12000.00")  # exact regardless of frequency
    # Dates are strictly ascending.
    dates = [p.date for p in sched]
    assert dates == sorted(dates)
    assert len(set(dates)) == expected_n


def test_final_period_carries_residual():
    # An odd balance/rate leaves a rounding residual that the last period must absorb.
    sched = amortization_schedule(
        D("10000"), D("0.055"), 24, datetime.date(2026, 1, 1), frequency="monthly"
    )
    assert sched[-1].balance == D("0")
    assert _sum_principal(sched) == D("10000.00")


def test_negative_amortization_raises():
    # A payment below the first period's interest never reduces the balance.
    with pytest.raises(ValueError, match="negative amortization"):
        amortization_schedule(
            D("100000"), D("0.12"), 360, datetime.date(2026, 1, 1),
            frequency="monthly", payment_amount=D("100"),
        )


def test_extra_principal_shortens_payoff_and_cuts_interest():
    base = amortization_schedule(
        D("100000"), D("0.06"), 360, datetime.date(2026, 1, 1), frequency="monthly"
    )
    faster = amortization_schedule(
        D("100000"), D("0.06"), 360, datetime.date(2026, 1, 1),
        frequency="monthly", extra_principal=D("200"),
    )
    assert len(faster) < len(base)
    assert sum((p.interest for p in faster), D("0")) < sum((p.interest for p in base), D("0"))
    assert _sum_principal(faster) == D("100000.00")


def test_suggest_split_normal_payment():
    # $10,000 @ 6% monthly: interest 50.00, principal = payment − interest.
    split = suggest_split(D("10000"), D("0.06"), D("599.55"), frequency="monthly")
    assert split == {"interest": D("50.00"), "principal": D("549.55")}


def test_suggest_split_payment_below_interest_is_all_interest():
    split = suggest_split(D("10000"), D("0.06"), D("30"), frequency="monthly")
    assert split == {"interest": D("30.00"), "principal": D("0.00")}


def test_suggest_split_caps_principal_at_balance():
    split = suggest_split(D("100"), D("0.06"), D("599.55"), frequency="monthly")
    assert split["principal"] == D("100.00")  # overpayment clamped to the balance


def test_add_months_clamps_day():
    assert add_months(datetime.date(2026, 1, 31), 1) == datetime.date(2026, 2, 28)
    assert add_months(datetime.date(2024, 1, 31), 1) == datetime.date(2024, 2, 29)  # leap year
    assert add_months(datetime.date(2026, 11, 15), 3) == datetime.date(2027, 2, 15)
