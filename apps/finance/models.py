"""Finance core — a double-entry general-ledger backbone (DESIGN §5 idioms, module 2).

This is a *backbone*: it is mostly invisible for now (no direct journal-entry UI, no fiscal-calendar
UI). Every future module posts to the ledger through `apps.finance.services` — never by writing
`JournalEntry`/`JournalLine` rows directly. The models here establish:

- **Currencies** + a per-line FX seam (transaction amount + rate + frozen base amount).
- A hierarchical **Chart of Accounts** with the five accounting elements (Asset/Liability/Equity/
  Revenue/Expense) and an explicit, stored `normal_side` (so contra accounts are possible).
- An invisible Jan–Dec **fiscal calendar** (FiscalYear + FiscalPeriod, auto-created on first post).
- A **General Ledger**: `JournalEntry` (header) + `JournalLine`, with a source-document GenericFK +
  idempotency key so subledgers can post without double-counting, and an optional Person/Org
  **counterparty** on each line so the ledger is natively queryable by party.

Balances are *computed* from posted lines by the service layer (DESIGN cadence: single source of
truth, no materialized balance table yet).
"""

from decimal import Decimal

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.models import SoftDeleteModel, TimeStampedModel

# Money is stored as Decimal(20,4): 4 dp covers every ISO-4217 currency + FX headroom.
# Per-currency display precision lives on Currency.decimal_places. FX rates get more
# precision. Never use float.
AMOUNT_MAX_DIGITS = 20
AMOUNT_DECIMALS = 4
RATE_MAX_DIGITS = 18
RATE_DECIMALS = 8
ZERO = Decimal("0")


class AccountType(models.TextChoices):
    ASSET = "ASSET", "Asset"
    LIABILITY = "LIABILITY", "Liability"
    EQUITY = "EQUITY", "Equity"
    REVENUE = "REVENUE", "Revenue"
    EXPENSE = "EXPENSE", "Expense"


class Side(models.TextChoices):
    DEBIT = "debit", "Debit"
    CREDIT = "credit", "Credit"


# Accounting convention: assets & expenses increase on the debit side; the rest on the credit side.
# Used only to *seed* an account's stored `normal_side` — never to derive it at query time (so a
# contra account, e.g. owner's drawings or accumulated depreciation, can flip its side explicitly).
DEBIT_NORMAL_TYPES = {AccountType.ASSET, AccountType.EXPENSE}


def default_side_for(account_type: str) -> str:
    return Side.DEBIT if account_type in DEBIT_NORMAL_TYPES else Side.CREDIT


class Currency(models.Model):
    """A currency the household transacts in. Reference catalog (seeded, `is_system` locked) — no
    soft-delete / history, mirroring `setup.Category`. The `code` (ISO-4217) is the natural PK."""

    code = models.CharField(max_length=3, primary_key=True)
    name = models.CharField(max_length=60)
    symbol = models.CharField(max_length=8, blank=True)
    decimal_places = models.PositiveSmallIntegerField(default=2)
    is_active = models.BooleanField(default=True)
    is_system = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["code"]
        verbose_name_plural = "currencies"

    def __str__(self) -> str:
        return self.code

    @property
    def label(self) -> str:
        return f"{self.code} — {self.name}"


class ExchangeRate(TimeStampedModel):
    """A dated rate to the household base currency: `rate` = units of base per 1 unit of `currency`.
    The service resolves the latest rate on/before a transaction date (base→base is always 1)."""

    currency = models.ForeignKey(Currency, on_delete=models.PROTECT, related_name="rates")
    as_of = models.DateField()
    rate = models.DecimalField(max_digits=RATE_MAX_DIGITS, decimal_places=RATE_DECIMALS)
    source = models.CharField(max_length=60, blank=True)

    class Meta:
        ordering = ["-as_of"]
        constraints = [
            models.UniqueConstraint(fields=["currency", "as_of"], name="uniq_rate_currency_date"),
        ]

    def __str__(self) -> str:
        return f"{self.currency_id} @ {self.as_of}: {self.rate}"


class Account(SoftDeleteModel):
    """A chart-of-accounts node. Hierarchical (self `parent`); only `is_postable` leaf accounts take
    journal lines, header accounts roll up their descendants. Seeded rows are `is_system` locked."""

    code = models.CharField(max_length=20)
    name = models.CharField(max_length=120)
    description = models.CharField(max_length=255, blank=True)
    type = models.CharField(max_length=12, choices=AccountType.choices)
    # Stored (not derived from `type`) so a contra account can flip its normal side without a
    # model change. Seeded via default_side_for(type).
    normal_side = models.CharField(max_length=6, choices=Side.choices)
    # A currency-denominated account (e.g. a EUR savings account); null = base/multi-currency.
    currency = models.ForeignKey(
        Currency, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    parent = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="children"
    )
    is_postable = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    is_system = models.BooleanField(default=False)
    # Stable role handle so services resolve special accounts by role, not fragile code
    # (e.g. "opening_balance_equity", "current_year_earnings", "fx_gain_loss").
    system_key = models.CharField(max_length=40, blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["code"]
        constraints = [
            models.UniqueConstraint(
                fields=["code"],
                condition=models.Q(deleted_at__isnull=True),
                name="uniq_account_code",
            ),
            models.UniqueConstraint(
                fields=["system_key"],
                condition=models.Q(deleted_at__isnull=True) & ~models.Q(system_key=""),
                name="uniq_account_system_key",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} {self.name}"

    @property
    def normal_sign(self) -> int:
        """+1 if the account's natural balance is a debit balance, else -1."""
        return 1 if self.normal_side == Side.DEBIT else -1

    @property
    def display(self) -> str:
        return f"{self.code} · {self.name}"


class FiscalYear(TimeStampedModel):
    """A calendar year of the ledger (Jan–Dec). `status` is the period-close seam; OPEN for now."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"

    year = models.IntegerField(unique=True)
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(max_length=6, choices=Status.choices, default=Status.OPEN)
    # Monotonic per-year journal-entry counter; bumped under select_for_update at post time.
    last_entry_no = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["year"]

    def __str__(self) -> str:
        return str(self.year)

    @property
    def is_closed(self) -> bool:
        return self.status == self.Status.CLOSED


class FiscalPeriod(TimeStampedModel):
    """A month within a fiscal year (period_no 1–12). Auto-created on first post; no UI for now."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"

    fiscal_year = models.ForeignKey(FiscalYear, on_delete=models.CASCADE, related_name="periods")
    period_no = models.PositiveSmallIntegerField()
    name = models.CharField(max_length=24)
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(max_length=6, choices=Status.choices, default=Status.OPEN)

    class Meta:
        ordering = ["fiscal_year", "period_no"]
        constraints = [
            models.UniqueConstraint(
                fields=["fiscal_year", "period_no"], name="uniq_period_year_no"
            ),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def is_closed(self) -> bool:
        return self.status == self.Status.CLOSED


class JournalEntry(SoftDeleteModel):
    """A general-ledger entry header. Balanced (Σ base debits == Σ base credits) and immutable once
    POSTED — a posted entry is reversed, never edited or deleted. Written only via the service."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        POSTED = "posted", "Posted"
        VOID = "void", "Void"

    class EntryType(models.TextChoices):
        STANDARD = "standard", "Standard"
        OPENING = "opening", "Opening balance"
        ADJUSTING = "adjusting", "Adjusting"
        CLOSING = "closing", "Closing"
        REVERSAL = "reversal", "Reversal"

    date = models.DateField()
    period = models.ForeignKey(FiscalPeriod, on_delete=models.PROTECT, related_name="entries")
    entry_type = models.CharField(
        max_length=10, choices=EntryType.choices, default=EntryType.STANDARD
    )
    # Human/audit-facing sequential number, unique per fiscal year; assigned only when POSTED.
    entry_no = models.PositiveIntegerField(null=True, blank=True)
    fiscal_year = models.SmallIntegerField(null=True, blank=True)  # denormalized; scopes entry_no
    description = models.CharField(max_length=255, blank=True)
    memo = models.TextField(blank=True)
    reference = models.CharField(max_length=60, blank=True)
    status = models.CharField(max_length=6, choices=Status.choices, default=Status.DRAFT)
    # Entry (presentation) currency; individual lines may transact in other currencies.
    currency = models.ForeignKey(Currency, on_delete=models.PROTECT, related_name="+")

    reversal_of = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="reversals"
    )
    is_reversed = models.BooleanField(default=False)

    external_key = models.CharField(max_length=200, blank=True, default="")  # idempotency

    # Source-document link (a subledger row that generated this entry). ContentType is a SHARED
    # (public) model; object_id points at a tenant-schema row, so `.source` resolves within the
    # active tenant only. SET_NULL keeps the ledger intact if a ContentType row is ever removed.
    source_content_type = models.ForeignKey(
        "contenttypes.ContentType",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    source_object_id = models.PositiveBigIntegerField(null=True, blank=True)
    source = GenericForeignKey("source_content_type", "source_object_id")

    posted_at = models.DateTimeField(null=True, blank=True)
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    history = HistoricalRecords()

    class Meta:
        ordering = ["-date", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["external_key"],
                condition=~models.Q(external_key=""),
                name="uniq_je_external_key",
            ),
            models.UniqueConstraint(
                fields=["fiscal_year", "entry_no"],
                condition=models.Q(entry_no__isnull=False),
                name="uniq_je_year_no",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "date"], name="je_status_date_idx"),
            models.Index(
                fields=["source_content_type", "source_object_id"], name="je_source_idx"
            ),
        ]
        verbose_name_plural = "journal entries"

    def __str__(self) -> str:
        label = f"JE#{self.entry_no}" if self.entry_no else f"JE(draft {self.pk})"
        return f"{label} {self.date} {self.description}".strip()


class JournalLine(TimeStampedModel):
    """One debit or credit posting. Immutable append-only (no history: the header carries audit and
    posted lines are never mutated). Carries both transaction-currency and frozen base amounts, plus
    an optional Person/Organization counterparty (the MyNestra 'how much with X' dimension)."""

    entry = models.ForeignKey(JournalEntry, on_delete=models.CASCADE, related_name="lines")
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="lines")
    currency = models.ForeignKey(Currency, on_delete=models.PROTECT, related_name="+")

    # Transaction-currency amounts (exactly one side > 0).
    debit = models.DecimalField(
        max_digits=AMOUNT_MAX_DIGITS, decimal_places=AMOUNT_DECIMALS, default=ZERO
    )
    credit = models.DecimalField(
        max_digits=AMOUNT_MAX_DIGITS, decimal_places=AMOUNT_DECIMALS, default=ZERO
    )
    fx_rate = models.DecimalField(
        max_digits=RATE_MAX_DIGITS, decimal_places=RATE_DECIMALS, default=Decimal("1")
    )
    # Functional/base-currency amounts, frozen at post time (so a posted entry always balances).
    base_debit = models.DecimalField(
        max_digits=AMOUNT_MAX_DIGITS, decimal_places=AMOUNT_DECIMALS, default=ZERO
    )
    base_credit = models.DecimalField(
        max_digits=AMOUNT_MAX_DIGITS, decimal_places=AMOUNT_DECIMALS, default=ZERO
    )
    memo = models.CharField(max_length=255, blank=True)

    # Optional counterparty (at most one, enforced by CHECK). PROTECT: don't orphan ledger history.
    person = models.ForeignKey(
        "contacts.Person",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="journal_lines",
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="journal_lines",
    )

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(debit__gt=0, credit=0) | models.Q(credit__gt=0, debit=0)
                ),
                name="journalline_one_side",
            ),
            models.CheckConstraint(
                condition=~models.Q(person__isnull=False, organization__isnull=False),
                name="journalline_one_party",
            ),
        ]

    def __str__(self) -> str:
        side = f"Dr {self.debit}" if self.debit else f"Cr {self.credit}"
        return f"{self.account_id} {side}"
