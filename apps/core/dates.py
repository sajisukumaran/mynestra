"""Upcoming-occurrence math for PartialDates (DESIGN §5 "upcoming-dates query").

A stored month/day recurs every year, so "birthdays / anniversaries in the next N days" must work
even when the year is unknown. This computes the *next* occurrence of a (month, day) on or after a
reference date. Dates with no month (fully-unknown, or year-only) can't be placed on the calendar
and are skipped (``None``). Month-only dates (day unknown) stay "current" for the whole month and
render ``XX-Mon``. Feb-29 clamps to the month's last day in a non-leap year.
"""

from __future__ import annotations

import calendar
import datetime
from dataclasses import dataclass


@dataclass(frozen=True)
class Occurrence:
    when: datetime.date  # the next calendar date this recurs on
    days_away: int       # whole days from the reference date (0 = today / this month)
    day_known: bool      # False for month-only dates (rendered XX-Mon, "this month")
    year: int | None     # the original stored year (for age math), if any
    month: int
    day: int | None


def _clamp_day(year: int, month: int, day: int) -> int:
    """Clamp a day to the month's length (Feb-29 → Feb-28 in a non-leap year)."""
    return min(day, calendar.monthrange(year, month)[1])


def next_occurrence(year, month, day, *, on=None, within_days=None) -> Occurrence | None:
    """Next recurrence of (month, day) on/after ``on`` (default today).

    Returns ``None`` when the month is unknown, or when a ``within_days`` window is given and the
    occurrence falls outside it. ``days_away`` is 0 for today (or the current month, month-only).
    """
    if not month:
        return None
    on = on or datetime.date.today()
    day_known = day is not None

    year_of = on.year
    when = datetime.date(year_of, month, _clamp_day(year_of, month, day or 1))
    if day_known:
        if when < on:  # already passed this year → roll to next year
            year_of += 1
            when = datetime.date(year_of, month, _clamp_day(year_of, month, day))
        days_away = (when - on).days
    else:
        # Month-only dates stay relevant for the whole month; only roll once the month has passed.
        if month < on.month:
            year_of += 1
            when = datetime.date(year_of, month, 1)
        days_away = max(0, (when - on).days)

    if within_days is not None and days_away > within_days:
        return None
    return Occurrence(
        when=when, days_away=days_away, day_known=day_known,
        year=year, month=month, day=day,
    )
