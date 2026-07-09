"""Unit matrix for apps.core.dates.next_occurrence (DESIGN §5 upcoming-dates query).

Pure date math — no DB. A fixed reference date keeps every case deterministic.
"""

import datetime

from apps.core.dates import next_occurrence

REF = datetime.date(2026, 3, 11)  # a non-leap year, mid-March


def test_full_date_upcoming_this_year():
    occ = next_occurrence(1974, 3, 14, on=REF)
    assert occ.when == datetime.date(2026, 3, 14)
    assert occ.days_away == 3
    assert occ.day_known is True
    assert occ.year == 1974


def test_today_is_zero_days_away():
    occ = next_occurrence(2000, 3, 11, on=REF)
    assert occ.when == REF
    assert occ.days_away == 0


def test_full_date_already_passed_rolls_to_next_year():
    occ = next_occurrence(1990, 1, 5, on=REF)
    assert occ.when == datetime.date(2027, 1, 5)
    assert occ.days_away > 250


def test_month_and_day_without_year():
    occ = next_occurrence(None, 4, 2, on=REF)
    assert occ.when == datetime.date(2026, 4, 2)
    assert occ.days_away == 22
    assert occ.day_known is True
    assert occ.year is None


def test_month_only_current_month_is_this_month():
    occ = next_occurrence(None, 3, None, on=REF)
    assert occ.day_known is False
    assert occ.when == datetime.date(2026, 3, 1)
    assert occ.days_away == 0  # relevant for the whole current month


def test_month_only_future_month():
    occ = next_occurrence(None, 5, None, on=REF)
    assert occ.day_known is False
    assert occ.when == datetime.date(2026, 5, 1)
    assert occ.days_away == 51


def test_month_only_past_month_rolls_to_next_year():
    occ = next_occurrence(None, 1, None, on=REF)
    assert occ.when == datetime.date(2027, 1, 1)
    assert occ.day_known is False


def test_year_only_is_skipped():
    assert next_occurrence(1974, None, None, on=REF) is None


def test_fully_unknown_is_skipped():
    assert next_occurrence(None, None, None, on=REF) is None


def test_feb_29_clamps_in_non_leap_year():
    # 2026 and 2027 are both non-leap → Feb-29 observed on Feb-28.
    occ = next_occurrence(2000, 2, 29, on=datetime.date(2026, 3, 1))
    assert occ.when.month == 2
    assert occ.when.day == 28
    assert occ.when.year == 2027  # this year's Feb already passed


def test_feb_29_stays_29_in_a_leap_year():
    occ = next_occurrence(2000, 2, 29, on=datetime.date(2024, 1, 1))
    assert occ.when == datetime.date(2024, 2, 29)


def test_within_days_window_excludes_far_dates():
    assert next_occurrence(None, 5, None, on=REF, within_days=30) is None  # 51 days out
    assert next_occurrence(1974, 3, 14, on=REF, within_days=30) is not None  # 3 days out
