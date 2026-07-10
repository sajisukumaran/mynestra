"""Contacts dashboard services (DESIGN §8): the upcoming-dates feed and the recents.

These read the tenant schema via the default managers (soft-deleted rows already excluded) and lean
on ``apps.core.dates.next_occurrence`` for the partial-date-aware "next N days" logic.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from django.utils import timezone

from apps.contacts.models import ImportantDate, Person
from apps.core.dates import Occurrence, next_occurrence
from apps.core.partialdate import MONTHS


def _ordinal(n: int) -> str:
    """1 → 1st, 2 → 2nd, 19 → 19th (used for 'Nth anniversary')."""
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


@dataclass
class UpcomingRow:
    occ: Occurrence
    person: Person
    title: str      # display name of the person the date belongs to
    subtitle: str   # e.g. "Turns 17 · Birthday", "19th anniversary", or a custom label
    kind: str       # "birthday" | "anniversary" | "date" — drives the row glyph/tint
    icon: str

    @property
    def when_display(self) -> str:
        """Compact 'day Mon' / 'Mon XX' for the row tail (XX = unknown day)."""
        mon = MONTHS[self.occ.month - 1]
        return f"{self.occ.day:02d} {mon}" if self.occ.day_known else f"{mon} XX"

    @property
    def badge_text(self) -> str:
        d = self.occ.days_away
        if d == 0:
            return "this month" if not self.occ.day_known else "today"
        if d == 1:
            return "tomorrow"
        return f"in {d} days"

    @property
    def badge_variant(self) -> str:
        return "warning" if self.occ.days_away <= 7 else ""


def _birthday_row(p: Person, occ: Occurrence) -> UpcomingRow:
    if p.dob_year:
        subtitle = f"Turns {occ.when.year - p.dob_year} · Birthday"
    else:
        subtitle = "Birthday"
    return UpcomingRow(occ, p, p.display_name, subtitle, "birthday", "cake")


def _anniversary_row(p: Person, occ: Occurrence) -> UpcomingRow:
    if p.anniversary_year:
        subtitle = f"{_ordinal(occ.when.year - p.anniversary_year)} anniversary"
    else:
        subtitle = "Anniversary"
    return UpcomingRow(occ, p, p.display_name, subtitle, "anniversary", "heart")


def upcoming_dates(within_days: int = 30, *, on: datetime.date | None = None) -> list[UpcomingRow]:
    """Birthdays (living only), anniversaries, and custom ImportantDates due in the next N days,
    partial-date-aware and sorted by how soon they fall (then by name)."""
    on = on or timezone.localdate()
    rows: list[UpcomingRow] = []

    for p in Person.objects.all():
        if not p.is_deceased:
            occ = next_occurrence(
                p.dob_year, p.dob_month, p.dob_day, on=on, within_days=within_days
            )
            if occ:
                rows.append(_birthday_row(p, occ))
        occ = next_occurrence(
            p.anniversary_year, p.anniversary_month, p.anniversary_day,
            on=on, within_days=within_days,
        )
        if occ:
            rows.append(_anniversary_row(p, occ))

    for d in ImportantDate.objects.select_related("person"):
        occ = next_occurrence(
            d.date_year, d.date_month, d.date_day, on=on, within_days=within_days
        )
        if occ:
            rows.append(UpcomingRow(occ, d.person, d.person.display_name, d.label, "date",
                                    "calendar-days"))

    rows.sort(key=lambda r: (r.occ.days_away, r.title.lower()))
    return rows


def count_birthdays(within_days: int = 30, *, on: datetime.date | None = None) -> int:
    """Living people with a birthday in the next N days (launcher + dashboard stat)."""
    on = on or timezone.localdate()
    return sum(
        1
        for p in Person.objects.filter(is_deceased=False)
        if next_occurrence(p.dob_year, p.dob_month, p.dob_day, on=on, within_days=within_days)
    )


def count_household_members() -> int:
    """People marked as belonging to the household (dashboard stat + pickers)."""
    return Person.objects.filter(is_household_member=True).count()


def count_upcoming(within_days: int = 30, *, on: datetime.date | None = None) -> int:
    """All upcoming dates (birthdays + anniversaries + important dates) in the next N days —
    the Contacts sidebar 'Important dates' badge."""
    return len(upcoming_dates(within_days, on=on))


def recent_people(days: int = 7, limit: int = 5):
    """People added in the last N days, newest first (dashboard 'Recently added')."""
    since = timezone.now() - datetime.timedelta(days=days)
    return list(Person.objects.filter(created_at__gte=since).order_by("-created_at")[:limit])


def recently_updated(limit: int = 5):
    """People edited after creation, most-recently-edited first ('Recently updated'). Skips rows
    only ever created (auto_now_add and auto_now land within a moment of each other on insert)."""
    people = Person.objects.order_by("-updated_at")[: limit * 3]
    edited = [p for p in people if (p.updated_at - p.created_at).total_seconds() >= 2]
    return edited[:limit]
