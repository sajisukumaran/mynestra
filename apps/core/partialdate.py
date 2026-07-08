"""PartialDate — the y/m/d nullable-smallint pattern (DESIGN §5).

Real-world dates a household records are often incomplete (unknown day, or only a year). Each
partial date is stored as three nullable smallints `<prefix>_year`, `<prefix>_month`, `<prefix>_day`
on the owning model (declared explicitly so migrations are clean). This module provides validation,
display (``XX``/``XXXX`` for missing parts), age, and a small value object.
"""

from __future__ import annotations

import calendar
import datetime
from dataclasses import dataclass

from django.core.exceptions import ValidationError

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _max_day(year: int | None, month: int) -> int:
    # When the year is unknown, allow 29 for February so leap-day birthdays validate; 2000 is a
    # leap year, so monthrange(2000, month) gives the max possible day for any month.
    return calendar.monthrange(year or 2000, month)[1]


def validate_partial_date(year, month, day):
    """Month 1–12; a day requires a month and must be valid for it; month/year may stand alone."""
    if month is not None and not (1 <= month <= 12):
        raise ValidationError("Month must be between 1 and 12.")
    if day is not None:
        if month is None:
            raise ValidationError("Choose a month before a day.")
        if not (1 <= day <= _max_day(year, month)):
            raise ValidationError("That day is not valid for the chosen month.")


def format_partial_date(year, month, day) -> str:
    """Render `14-Mar-1974`, `14-Mar-XXXX`, `XX-Mar-1974`, `XX-XX-1974` (empty if nothing set)."""
    if not (year or month or day):
        return ""
    d = f"{day:02d}" if day else "XX"
    m = MONTHS[month - 1] if month else "XX"
    y = f"{year:04d}" if year else "XXXX"
    return f"{d}-{m}-{y}"


def partial_date_age(year, month, day, on: datetime.date | None = None) -> int | None:
    """Whole years from the date to `on` (default today). None when the year is unknown."""
    if not year:
        return None
    on = on or datetime.date.today()
    month = month or 1
    day = min(day or 1, _max_day(year, month))
    born = datetime.date(year, month, day)
    age = on.year - born.year - ((on.month, on.day) < (born.month, born.day))
    return age if age >= 0 else None


@dataclass(frozen=True)
class PartialDate:
    year: int | None = None
    month: int | None = None
    day: int | None = None

    @classmethod
    def from_instance(cls, obj, prefix: str) -> PartialDate:
        return cls(
            getattr(obj, f"{prefix}_year"),
            getattr(obj, f"{prefix}_month"),
            getattr(obj, f"{prefix}_day"),
        )

    @property
    def is_set(self) -> bool:
        return bool(self.year or self.month or self.day)

    @property
    def display(self) -> str:
        return format_partial_date(self.year, self.month, self.day)

    @property
    def age(self) -> int | None:
        return partial_date_age(self.year, self.month, self.day)

    def clean(self):
        validate_partial_date(self.year, self.month, self.day)

    def __str__(self) -> str:
        return self.display
