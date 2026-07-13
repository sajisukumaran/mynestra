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
from simple_history.models import HistoricalRecords

from apps.core.models import SoftDeleteModel, TimeStampedModel
from apps.finance.models import AMOUNT_DECIMALS, AMOUNT_MAX_DIGITS


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


class Item(SoftDeleteModel):
    """A catalogued purchasable item (a good or a service). Bill lines reference it, giving search
    by UPC/SKU, spend-by-item, and price history. Mirrors the Investments Security master. Carries
    the default GL homes a purchase posts to (an expense account, or an asset account when the item
    is a durable good you capitalize for warranty tracking)."""

    class Kind(models.TextChoices):
        GOOD = "good", "Good"
        SERVICE = "service", "Service"

    name = models.CharField(max_length=160)
    description = models.CharField(max_length=255, blank=True)
    upc = models.CharField(max_length=40, blank=True)  # universal barcode; per-store SKUs below
    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.GOOD)
    unit = models.CharField(max_length=24, blank=True)  # each / box / kg / hour…
    # Default posting homes for a purchase of this item.
    default_account = models.ForeignKey(
        "finance.Account", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    # When true, a bill line for this item defaults to capitalizing to `asset_account`.
    capitalize_default = models.BooleanField(default=False)
    asset_account = models.ForeignKey(
        "finance.Account", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["upc"], name="payables_item_upc_idx")]

    def __str__(self) -> str:
        return self.name

    @property
    def kind_label(self) -> str:
        return self.get_kind_display()

    @property
    def is_service(self) -> bool:
        return self.kind == self.Kind.SERVICE

    @property
    def tint(self) -> str:
        return "violet" if self.is_service else "teal"

    @property
    def latest_price(self):
        """The last recorded price from this item's most recently updated store SKU (or None)."""
        sku = self.skus.exclude(last_price__isnull=True).order_by("-updated_at").first()
        return sku.last_price if sku else None


class ItemSku(TimeStampedModel):
    """A store-specific stock-keeping unit for an item. SKUs are per store; the UPC (on `Item`) is
    universal. `store` links an Organization when known, else `store_name` is free text."""

    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="skus")
    store = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    store_name = models.CharField(max_length=120, blank=True)
    sku = models.CharField(max_length=60)
    last_price = models.DecimalField(
        max_digits=AMOUNT_MAX_DIGITS, decimal_places=AMOUNT_DECIMALS, null=True, blank=True
    )
    note = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["store_name", "sku"]
        constraints = [
            models.UniqueConstraint(
                fields=["item", "store", "sku"], name="uniq_itemsku_item_store_sku"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.sku} @ {self.store_display}"

    @property
    def store_display(self) -> str:
        if self.store_id:
            return self.store.name
        return self.store_name or "—"


class VendorProfile(SoftDeleteModel):
    """A vendor — someone you owe. Points to a Person OR an Organization (exactly one), plus the
    household's purchase defaults for them. Organization vendors are additionally tagged the locked
    'Vendor' category for cross-module discoverability; Person vendors just carry this profile."""

    person = models.ForeignKey(
        "contacts.Person", on_delete=models.PROTECT, null=True, blank=True,
        related_name="vendor_profile",
    )
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.PROTECT, null=True, blank=True,
        related_name="vendor_profile",
    )
    default_terms = models.ForeignKey(
        PaymentTerm, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    default_expense_account = models.ForeignKey(
        "finance.Account", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    account_number = models.CharField(max_length=60, blank=True)  # your account # with them
    currency = models.ForeignKey(
        "finance.Currency", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["-id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(person__isnull=False, organization__isnull=True)
                    | models.Q(person__isnull=True, organization__isnull=False)
                ),
                name="vendorprofile_one_party",
            ),
            models.UniqueConstraint(
                fields=["person"],
                condition=models.Q(deleted_at__isnull=True, person__isnull=False),
                name="uniq_vendor_person",
            ),
            models.UniqueConstraint(
                fields=["organization"],
                condition=models.Q(deleted_at__isnull=True, organization__isnull=False),
                name="uniq_vendor_org",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def party(self):
        return self.person or self.organization

    @property
    def party_kind(self) -> str:
        return "person" if self.person_id else "organization"

    @property
    def name(self) -> str:
        party = self.party
        for attr in ("display_name", "full_name", "name"):
            val = getattr(party, attr, "")
            if val:
                return val
        return str(party)

    @property
    def initials(self) -> str:
        return getattr(self.party, "initials", "?")

    @property
    def tint(self) -> str:
        return getattr(self.party, "avatar_tint", "slate")
