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

from django.contrib.contenttypes.fields import GenericForeignKey
from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.models import SoftDeleteModel, TimeStampedModel
from apps.finance.models import AMOUNT_DECIMALS, AMOUNT_MAX_DIGITS, ZERO


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


class AssetItem(SoftDeleteModel):
    """A durable purchase capitalized from a bill line (electronics/appliances/furniture), tracked
    for warranty. Held at cost on the balance sheet (no depreciation); a lightweight register that
    seams into a future Assets module. Created/maintained by `services.post_bill`, not hand-entered.
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        DISPOSED = "disposed", "Disposed"

    name = models.CharField(max_length=160)
    vendor_name = models.CharField(max_length=160, blank=True)
    serial_number = models.CharField(max_length=80, blank=True)
    purchase_date = models.DateField(null=True, blank=True)
    cost = models.DecimalField(max_digits=AMOUNT_MAX_DIGITS, decimal_places=AMOUNT_DECIMALS,
                               default=ZERO)
    warranty_start = models.DateField(null=True, blank=True)
    warranty_end = models.DateField(null=True, blank=True)
    gl_account = models.ForeignKey(
        "finance.Account", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    bill_line = models.OneToOneField(
        "payables.BillLine", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="asset_item",
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    notes = models.TextField(blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["-purchase_date", "name"]

    def __str__(self) -> str:
        return self.name

    @property
    def warranty_active(self) -> bool:
        return bool(self.warranty_end and self.warranty_end >= datetime.date.today())


class Bill(SoftDeleteModel):
    """A vendor bill (accrual accounts-payable document). Posts DR expense/asset / CR Accounts
    Payable at bill date via `services.post_bill`; a Payment later clears it. The vendor is a Person
    OR an Organization (exactly one). Bills post on save and are edited in place (no reversal);
    `is_locked` + `source` mark a bill generated by another module (read-only here)."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        PARTIALLY_PAID = "partially_paid", "Partially paid"
        PAID = "paid", "Paid"
        VOID = "void", "Void"

    vendor_person = models.ForeignKey(
        "contacts.Person", on_delete=models.PROTECT, null=True, blank=True, related_name="bills"
    )
    vendor_organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.PROTECT, null=True, blank=True,
        related_name="bills",
    )
    number = models.PositiveIntegerField(null=True, blank=True)  # internal sequential doc number
    vendor_ref = models.CharField(max_length=80, blank=True)  # the vendor's own invoice number
    bill_date = models.DateField()
    terms = models.ForeignKey(
        PaymentTerm, on_delete=models.PROTECT, null=True, blank=True, related_name="bills"
    )
    due_date = models.DateField(null=True, blank=True)
    currency = models.ForeignKey(
        "finance.Currency", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    notes = models.TextField(blank=True)

    # Order / shipment metadata (flat; no separate Order/Shipment subsystem for now).
    store_name = models.CharField(max_length=120, blank=True)
    order_number = models.CharField(max_length=80, blank=True)
    order_date = models.DateField(null=True, blank=True)
    tracking_number = models.CharField(max_length=120, blank=True)
    carrier = models.CharField(max_length=60, blank=True)
    ship_date = models.DateField(null=True, blank=True)
    delivery_date = models.DateField(null=True, blank=True)

    # Cross-module "locked bill" seam: another module can generate a read-only, system-owned bill.
    is_locked = models.BooleanField(default=False)
    source_content_type = models.ForeignKey(
        "contenttypes.ContentType", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    source_object_id = models.PositiveBigIntegerField(null=True, blank=True)
    source = GenericForeignKey("source_content_type", "source_object_id")

    journal_entry = models.ForeignKey(
        "finance.JournalEntry", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    posting_version = models.PositiveIntegerField(default=1)

    history = HistoricalRecords()

    class Meta:
        ordering = ["-bill_date", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(vendor_person__isnull=False, vendor_organization__isnull=True)
                    | models.Q(vendor_person__isnull=True, vendor_organization__isnull=False)
                ),
                name="bill_one_vendor",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.number is None:
            top = Bill.all_objects.aggregate(m=models.Max("number"))["m"] or 0
            self.number = top + 1
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.bill_number

    # --- vendor party ---
    @property
    def vendor(self):
        return self.vendor_person or self.vendor_organization

    @property
    def vendor_kind(self) -> str:
        return "person" if self.vendor_person_id else "organization"

    @property
    def vendor_name(self) -> str:
        party = self.vendor
        for attr in ("display_name", "full_name", "name"):
            val = getattr(party, attr, "")
            if val:
                return val
        return str(party)

    @property
    def vendor_tint(self) -> str:
        return getattr(self.vendor, "avatar_tint", "slate")

    @property
    def vendor_initials(self) -> str:
        return getattr(self.vendor, "initials", "?")

    @property
    def bill_number(self) -> str:
        return f"BILL-{self.number:05d}" if self.number else "BILL"

    # --- totals (computed from lines) ---
    def _sum(self, *types) -> object:
        return sum((li.amount for li in self.lines.all() if li.line_type in types), ZERO)

    @property
    def subtotal(self):
        return self._sum(BillLine.LineType.ITEM, BillLine.LineType.SERVICE,
                         BillLine.LineType.EXPENSE)

    @property
    def shipping_total(self):
        return self._sum(BillLine.LineType.SHIPPING)

    @property
    def tax_total(self):
        return self._sum(BillLine.LineType.TAX)

    @property
    def discount_total(self):
        # Negative (discount lines carry a negative amount).
        return self._sum(BillLine.LineType.DISCOUNT)

    @property
    def total(self):
        # Honors the `total_agg` annotation from `services.bills_with_totals`, so loops over
        # annotated querysets don't fire a lines query per bill.
        agg = getattr(self, "total_agg", None)
        if agg is not None:
            return agg
        return sum((li.amount for li in self.lines.all()), ZERO)

    @property
    def amount_paid(self):
        # Honors the `paid_agg` annotation from `services.bills_with_totals` (see `total`).
        agg = getattr(self, "paid_agg", None)
        if agg is not None:
            return agg
        return sum((a.amount for a in self.allocations.all()), ZERO)

    @property
    def balance_due(self):
        return self.total - self.amount_paid

    @property
    def is_overdue(self) -> bool:
        return bool(
            self.due_date
            and self.status in (self.Status.OPEN, self.Status.PARTIALLY_PAID)
            and self.due_date < datetime.date.today()
        )

    @property
    def days_to_due(self):
        if not self.due_date:
            return None
        return (self.due_date - datetime.date.today()).days


class BillLine(TimeStampedModel):
    """One line of a bill. `line_type` picks the nature + default posting: item/service/expense DR
    an expense (or a capitalized asset), shipping/tax DR their own accounts, discount CRs Purchase
    Discounts. Per-line tax/discount fold into the line's cost."""

    class LineType(models.TextChoices):
        ITEM = "item", "Item"
        SERVICE = "service", "Service"
        EXPENSE = "expense", "Expense"
        SHIPPING = "shipping", "Shipping"
        TAX = "tax", "Tax"
        DISCOUNT = "discount", "Discount"

    bill = models.ForeignKey(Bill, on_delete=models.CASCADE, related_name="lines")
    line_type = models.CharField(max_length=10, choices=LineType.choices, default=LineType.EXPENSE)
    item = models.ForeignKey(
        Item, on_delete=models.SET_NULL, null=True, blank=True, related_name="bill_lines"
    )
    description = models.CharField(max_length=255, blank=True)
    quantity = models.DecimalField(max_digits=AMOUNT_MAX_DIGITS, decimal_places=AMOUNT_DECIMALS,
                                   default=Decimal("1"))
    unit_price = models.DecimalField(max_digits=AMOUNT_MAX_DIGITS, decimal_places=AMOUNT_DECIMALS,
                                     default=ZERO)
    # line_discount / line_tax fold into this line's own cost (tax-inclusive, discount-reduced).
    line_discount = models.DecimalField(
        max_digits=AMOUNT_MAX_DIGITS, decimal_places=AMOUNT_DECIMALS, default=ZERO
    )
    line_tax = models.DecimalField(
        max_digits=AMOUNT_MAX_DIGITS, decimal_places=AMOUNT_DECIMALS, default=ZERO
    )
    account = models.ForeignKey(
        "finance.Account", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    capitalize = models.BooleanField(default=False)
    asset_serial = models.CharField(max_length=80, blank=True)
    warranty_end = models.DateField(null=True, blank=True)
    order = models.PositiveIntegerField(default=0)

    history = HistoricalRecords()

    class Meta:
        ordering = ["bill", "order", "id"]

    def __str__(self) -> str:
        return f"{self.get_line_type_display()}: {self.description}"

    @property
    def base_amount(self):
        return self.quantity * self.unit_price - self.line_discount + self.line_tax

    @property
    def amount(self):
        """Signed contribution to the bill total (discount lines are negative).

        `services.bills_with_totals` computes this same formula in SQL — any change here MUST be
        mirrored there."""
        base = self.base_amount
        return -base if self.line_type == self.LineType.DISCOUNT else base


class Payment(SoftDeleteModel):
    """A payment that settles one or more bills of a single vendor. It draws from a funding source:
    a bank account (creates a native bank withdrawal), a credit card (a native card charge), or cash
    (posts DR Accounts Payable / CR cash directly). Allocations link it to the bills it clears."""

    class Funding(models.TextChoices):
        BANK = "bank", "Bank account"
        CARD = "card", "Credit card"
        CASH = "cash", "Cash / other"

    vendor_person = models.ForeignKey(
        "contacts.Person", on_delete=models.PROTECT, null=True, blank=True, related_name="payments"
    )
    vendor_organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.PROTECT, null=True, blank=True,
        related_name="payments",
    )
    number = models.PositiveIntegerField(null=True, blank=True)
    date = models.DateField()
    amount = models.DecimalField(max_digits=AMOUNT_MAX_DIGITS, decimal_places=AMOUNT_DECIMALS,
                                 default=ZERO)
    funding_kind = models.CharField(max_length=8, choices=Funding.choices, default=Funding.CASH)
    bank_account = models.ForeignKey(
        "banking.BankAccount", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    credit_card = models.ForeignKey(
        "cards.CreditCard", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    cash_account = models.ForeignKey(
        "finance.Account", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    reference = models.CharField(max_length=80, blank=True)
    notes = models.TextField(blank=True)
    # The native funding transaction this payment created (kept truthful in that module's register).
    bank_txn = models.ForeignKey(
        "banking.BankTransaction", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    card_txn = models.ForeignKey(
        "cards.CreditCardTransaction", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    journal_entry = models.ForeignKey(
        "finance.JournalEntry", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    posting_version = models.PositiveIntegerField(default=1)

    history = HistoricalRecords()

    class Meta:
        ordering = ["-date", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(vendor_person__isnull=False, vendor_organization__isnull=True)
                    | models.Q(vendor_person__isnull=True, vendor_organization__isnull=False)
                ),
                name="payment_one_vendor",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.number is None:
            top = Payment.all_objects.aggregate(m=models.Max("number"))["m"] or 0
            self.number = top + 1
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.payment_number

    @property
    def payment_number(self) -> str:
        return f"PMT-{self.number:05d}" if self.number else "PMT"

    @property
    def vendor(self):
        return self.vendor_person or self.vendor_organization

    @property
    def vendor_name(self) -> str:
        party = self.vendor
        for attr in ("display_name", "full_name", "name"):
            val = getattr(party, attr, "")
            if val:
                return val
        return str(party)

    @property
    def funding_label(self) -> str:
        if self.funding_kind == self.Funding.BANK and self.bank_account_id:
            return self.bank_account.nickname
        if self.funding_kind == self.Funding.CARD and self.credit_card_id:
            return self.credit_card.nickname
        return "Cash / other"


class PaymentAllocation(TimeStampedModel):
    """How much of a payment settles a given bill. Drives each bill's amount-paid / status."""

    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name="allocations")
    bill = models.ForeignKey(Bill, on_delete=models.CASCADE, related_name="allocations")
    amount = models.DecimalField(max_digits=AMOUNT_MAX_DIGITS, decimal_places=AMOUNT_DECIMALS,
                                 default=ZERO)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["payment", "bill"], name="uniq_alloc_payment_bill"),
        ]

    def __str__(self) -> str:
        return f"{self.payment_id}->{self.bill_id}: {self.amount}"
