"""Payables (module 6) — accrual accounts-payable: vendor bills, payments, an item/SKU catalog, and
their supporting catalogs.

A Bill posts `DR expense/asset / CR Accounts Payable` at bill date; a Payment later clears AP.
Everything reaches the general ledger only through `apps.payables.services` (never by writing
`JournalEntry`/`JournalLine` rows directly). This module starts with the neutral `PaymentTerm`
catalog (also reusable by a future Receivables module); bills/payments/items land in later commits.
"""

import calendar
import datetime
from decimal import Decimal

from django.db import models

from apps.core.models import TimeStampedModel


class PaymentTerm(TimeStampedModel):
    """A vendor payment term: how a bill's due date (and any early-payment discount) is derived from
    its bill date. Seeded system terms (`is_system`) are locked in Setup; households add their own.
    Mirrors the sibling Setup catalogs (Category, RelationshipType): a plain lockable reference row.
    """

    class Kind(models.TextChoices):
        DUE_ON_RECEIPT = "due_on_receipt", "Due on receipt"
        NET_DAYS = "net_days", "Net N days"
        DAY_OF_MONTH = "day_of_month", "Day of month"
        SPECIFIC_DATE = "specific_date", "Specific date"

    name = models.CharField(max_length=60)
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.NET_DAYS)
    # NET_DAYS: due = bill_date + net_days.
    net_days = models.PositiveSmallIntegerField(default=0)
    # DAY_OF_MONTH: due = the next occurrence of this day (1–31, clamped per month).
    day_of_month = models.PositiveSmallIntegerField(null=True, blank=True)
    # Early-payment discount (e.g. 2/10 net 30): take `discount_percent`% if paid within
    # `discount_days` days of the bill date.
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"))
    discount_days = models.PositiveSmallIntegerField(default=0)
    is_system = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["name"], name="uniq_payment_term_name"),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def has_discount(self) -> bool:
        return self.discount_percent > 0 and self.discount_days > 0

    @property
    def rule_display(self) -> str:
        if self.kind == self.Kind.DUE_ON_RECEIPT:
            return "Due on receipt"
        if self.kind == self.Kind.NET_DAYS:
            return f"Net {self.net_days} days"
        if self.kind == self.Kind.DAY_OF_MONTH:
            return f"Day {self.day_of_month} of the month"
        return "Specific date (set per bill)"

    @property
    def discount_display(self) -> str:
        if not self.has_discount:
            return ""
        pct = self.discount_percent.normalize()
        return f"{pct}% if paid within {self.discount_days} days"

    def due_date_for(self, bill_date: datetime.date) -> datetime.date | None:
        """The due date for a bill dated `bill_date` under this term. Returns None for SPECIFIC_DATE
        (the caller keeps the explicitly-entered due date)."""
        if self.kind == self.Kind.DUE_ON_RECEIPT:
            return bill_date
        if self.kind == self.Kind.NET_DAYS:
            return bill_date + datetime.timedelta(days=self.net_days)
        if self.kind == self.Kind.DAY_OF_MONTH and self.day_of_month:
            return self._next_day_of_month(bill_date)
        return None  # SPECIFIC_DATE (or an under-specified term): caller supplies the due date

    def _next_day_of_month(self, bill_date: datetime.date) -> datetime.date:
        """The next occurrence of `day_of_month` on/after `bill_date` (clamped to month length)."""
        dom = min(self.day_of_month, calendar.monthrange(bill_date.year, bill_date.month)[1])
        candidate = bill_date.replace(day=dom)
        if candidate >= bill_date:
            return candidate
        year = bill_date.year + (1 if bill_date.month == 12 else 0)
        month = 1 if bill_date.month == 12 else bill_date.month + 1
        dom = min(self.day_of_month, calendar.monthrange(year, month)[1])
        return datetime.date(year, month, dom)

    def discount_deadline(self, bill_date: datetime.date) -> datetime.date | None:
        """The last day an early-payment discount can be taken, or None if there's no discount."""
        if not self.has_discount:
            return None
        return bill_date + datetime.timedelta(days=self.discount_days)
