"""Cards — household credit cards (a liability ledger) and debit cards (a registry).

The Cards module is the second consumer of the finance GL backbone (after Banking). A **credit
card** is an account: each `CreditCard` owns one postable `finance.Account` nested under the
`2100 Credit Cards` header (normal_side=credit), and every `CreditCardTransaction` posts a balanced
journal entry through `apps.cards.services` — charges/interest/fees increase the balance owed,
payments/refunds/credits decrease it. Balances are computed from posted lines (no stored balance).

A **debit card** is NOT an account — it is a payment instrument on a `banking.BankAccount`. It has
no ledger of its own; spending against it is a bank withdrawal (optionally tagged with the card). So
`DebitCard` is a registry row (network, last-4, expiry, holder, linked account, limit) whose balance
simply delegates to its bank account.

Soft-deletable + audited like every tenant model (DESIGN §5).
"""

from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.models import SoftDeleteModel, TimeStampedModel
from apps.core.partialdate import PartialDate
from apps.finance.models import AMOUNT_DECIMALS, AMOUNT_MAX_DIGITS, ZERO


class CardNetwork(models.TextChoices):
    VISA = "visa", "Visa"
    MASTERCARD = "mastercard", "Mastercard"
    AMEX = "amex", "American Express"
    DISCOVER = "discover", "Discover"
    RUPAY = "rupay", "RuPay"
    OTHER = "other", "Other"


class CardTxnType(models.TextChoices):
    # OPENING is created only by the card setup form (opening balance owed), not the txn picker.
    OPENING = "opening", "Opening balance"
    CHARGE = "charge", "Charge"
    PAYMENT = "payment", "Payment"
    INTEREST = "interest", "Interest"
    FEE = "fee", "Fee"
    REFUND = "refund", "Refund"
    CREDIT = "credit", "Statement credit"


# Transaction types that INCREASE the balance owed (a credit to the liability); the rest reduce it.
# Drives the signed running balance (positive = you owe more).
INCREASE_TYPES = frozenset(
    {CardTxnType.OPENING, CardTxnType.CHARGE, CardTxnType.INTEREST, CardTxnType.FEE}
)

# Lucide glyphs per transaction type — up = owe more, down = owe less. All in _icon_sprite.html.
TXN_GLYPHS = {
    CardTxnType.OPENING: "pin",
    CardTxnType.CHARGE: "arrow-up",
    CardTxnType.PAYMENT: "arrow-down",
    CardTxnType.INTEREST: "coins",
    CardTxnType.FEE: "arrow-up",
    CardTxnType.REFUND: "arrow-down",
    CardTxnType.CREDIT: "arrow-down",
}


class CreditCard(SoftDeleteModel):
    """A household credit card. Its balance owed lives in the general ledger via `gl_account`
    (a liability under the 2100 header); this row carries the human-facing metadata + limits."""

    issuer = models.ForeignKey(
        "organizations.Organization", on_delete=models.PROTECT, related_name="credit_cards"
    )
    network = models.CharField(
        max_length=12, choices=CardNetwork.choices, default=CardNetwork.OTHER
    )
    nickname = models.CharField(max_length=120)
    number = models.CharField(max_length=40, blank=True)  # displayed masked (last 4)
    currency = models.ForeignKey("finance.Currency", on_delete=models.PROTECT, related_name="+")

    # The dedicated postable ledger account (one per card); created by
    # apps.cards.services.ensure_gl_account after the row is first saved.
    gl_account = models.OneToOneField(
        "finance.Account",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="credit_card",
    )

    credit_limit = models.DecimalField(
        max_digits=AMOUNT_MAX_DIGITS, decimal_places=AMOUNT_DECIMALS, null=True, blank=True
    )
    statement_day = models.SmallIntegerField(null=True, blank=True)  # day of month (1–31)
    due_day = models.SmallIntegerField(null=True, blank=True)
    apr = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)  # percent

    is_active = models.BooleanField(default=True)

    opened_year = models.SmallIntegerField(null=True, blank=True)
    opened_month = models.SmallIntegerField(null=True, blank=True)
    opened_day = models.SmallIntegerField(null=True, blank=True)
    closed_year = models.SmallIntegerField(null=True, blank=True)
    closed_month = models.SmallIntegerField(null=True, blank=True)
    closed_day = models.SmallIntegerField(null=True, blank=True)

    notes = models.TextField(blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["nickname"]

    def __str__(self) -> str:
        return self.nickname

    @property
    def display(self) -> str:
        return self.nickname

    @property
    def masked_number(self) -> str:
        n = (self.number or "").strip()
        if not n:
            return ""
        return f"••••{n[-4:]}" if len(n) > 4 else n

    @property
    def network_label(self) -> str:
        return self.get_network_display()

    @property
    def balance(self):
        """Current balance owed, in the base currency (positive = you owe). Computed from the GL.
        Honors a value stamped by `services.attach_balances` so loops don't aggregate per row."""
        stamped = getattr(self, "_balance", None)
        if stamped is not None:
            return stamped
        if self.gl_account_id is None:
            return ZERO
        from apps.finance.services import account_balance

        return account_balance(self.gl_account)

    @property
    def native_balance(self):
        """Honors a value stamped by `services.attach_balances`."""
        stamped = getattr(self, "_native_balance", None)
        if stamped is not None:
            return stamped
        if self.gl_account_id is None:
            return None
        from apps.finance.services import account_native_balance

        return account_native_balance(self.gl_account)

    @property
    def display_balance(self):
        """Balance owed in the card's own currency for display (never None; zero before posting)."""
        nb = self.native_balance
        return nb if nb is not None else ZERO

    @property
    def available_credit(self):
        """Credit limit minus the balance owed; None when no limit is set."""
        if self.credit_limit is None:
            return None
        return self.credit_limit - self.balance

    @property
    def utilization(self):
        """Percent of the credit limit currently used (0–100+), or None when no limit is set."""
        if not self.credit_limit or self.credit_limit <= 0:
            return None
        return (self.balance / self.credit_limit) * 100

    @property
    def utilization_tint(self) -> str:
        """c-meter tint for the utilization bar (danger/warning above thresholds)."""
        u = self.utilization
        if u is None:
            return ""
        if u >= 90:
            return "danger"
        if u >= 70:
            return "warning"
        return ""

    @property
    def opened(self) -> PartialDate:
        return PartialDate.from_instance(self, "opened")

    @property
    def closed(self) -> PartialDate:
        return PartialDate.from_instance(self, "closed")

    @property
    def is_closed(self) -> bool:
        return self.closed.is_set


class CreditCardHolder(TimeStampedModel):
    """A household member who holds a credit card (primary or authorized user). Multiple holders
    model a joint / authorized-user card; one may be flagged primary. Unique per (card, person)."""

    card = models.ForeignKey(CreditCard, on_delete=models.CASCADE, related_name="holders")
    person = models.ForeignKey(
        "contacts.Person", on_delete=models.PROTECT, related_name="card_holdings"
    )
    is_primary = models.BooleanField(default=False)

    class Meta:
        ordering = ["-is_primary", "id"]
        constraints = [
            models.UniqueConstraint(fields=["card", "person"], name="creditcardholder_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.person} @ {self.card}"


class CreditCardTransaction(SoftDeleteModel):
    """One line in a credit card's register. Posts a balanced journal entry via
    `apps.cards.services` (kept in sync through `journal_entry` + `posting_version`)."""

    card = models.ForeignKey(CreditCard, on_delete=models.CASCADE, related_name="transactions")
    txn_type = models.CharField(max_length=12, choices=CardTxnType.choices)
    date = models.DateField()
    amount = models.DecimalField(max_digits=AMOUNT_MAX_DIGITS, decimal_places=AMOUNT_DECIMALS)

    # Contra account for charges/refunds (the expense "category"). Fixed contras (interest/fee/
    # payment/credit) are resolved by the service, so this stays null for them.
    category_account = models.ForeignKey(
        "finance.Account", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    # Payment source: a tracked bank account (auto-matched), or a free-text external counterpart.
    counter_account = models.ForeignKey(
        "banking.BankAccount",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="card_payments",
    )
    counter_external = models.CharField(max_length=160, blank=True)

    # Optional payee (at most one) — flows to the GL line's counterparty dimension (the merchant).
    payee_person = models.ForeignKey(
        "contacts.Person", on_delete=models.PROTECT, null=True, blank=True, related_name="card_txns"
    )
    payee_organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="card_txns",
    )

    memo = models.CharField(max_length=255, blank=True)
    reference = models.CharField(max_length=60, blank=True)
    cleared = models.BooleanField(default=False)  # reconciliation-lite; never affects the GL

    journal_entry = models.ForeignKey(
        "finance.JournalEntry", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    posting_version = models.PositiveIntegerField(default=1)

    history = HistoricalRecords()

    class Meta:
        ordering = ["-date", "-id"]
        indexes = [
            # The register's display/window order — per-card (date, id) scans read the index.
            models.Index(fields=["card", "date", "id"], name="cardtxn_card_date_id"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(
                    payee_person__isnull=False, payee_organization__isnull=False
                ),
                name="cardtransaction_one_payee",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="cardtransaction_amount_positive"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_txn_type_display()} {self.amount} on {self.date}"

    @property
    def type_label(self) -> str:
        return self.get_txn_type_display()

    @property
    def type_glyph(self) -> str:
        return TXN_GLYPHS.get(self.txn_type, "circle")

    @property
    def increases_balance(self) -> bool:
        return self.txn_type in INCREASE_TYPES

    @property
    def signed_amount(self):
        """Positive when it increases the balance owed, negative when it reduces it."""
        return self.amount if self.increases_balance else -self.amount

    @property
    def payee(self):
        return self.payee_person or self.payee_organization


class DebitCard(SoftDeleteModel):
    """A debit card — a payment instrument on a bank account, with no ledger of its own. Spending is
    a bank withdrawal; this row records network/last-4/expiry/holder/limit and links to the account.
    Its balance delegates to the linked `banking.BankAccount`."""

    bank_account = models.ForeignKey(
        "banking.BankAccount", on_delete=models.PROTECT, related_name="debit_cards"
    )
    network = models.CharField(
        max_length=12, choices=CardNetwork.choices, default=CardNetwork.OTHER
    )
    nickname = models.CharField(max_length=120)
    number = models.CharField(max_length=40, blank=True)  # displayed masked (last 4)
    holder = models.ForeignKey(
        "contacts.Person",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="debit_cards",
    )
    expiry_month = models.SmallIntegerField(null=True, blank=True)
    expiry_year = models.SmallIntegerField(null=True, blank=True)
    daily_limit = models.DecimalField(
        max_digits=AMOUNT_MAX_DIGITS, decimal_places=AMOUNT_DECIMALS, null=True, blank=True
    )
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["nickname"]

    def __str__(self) -> str:
        return self.nickname

    @property
    def display(self) -> str:
        return self.nickname

    @property
    def masked_number(self) -> str:
        n = (self.number or "").strip()
        if not n:
            return ""
        return f"••••{n[-4:]}" if len(n) > 4 else n

    @property
    def network_label(self) -> str:
        return self.get_network_display()

    @property
    def expiry_display(self) -> str:
        if not self.expiry_month or not self.expiry_year:
            return ""
        return f"{self.expiry_month:02d}/{str(self.expiry_year)[-2:]}"

    @property
    def balance(self):
        """The linked bank account's balance (base currency)."""
        return self.bank_account.balance

    @property
    def display_balance(self):
        return self.bank_account.display_balance


def signed_amount_sql():
    """SQL twin of `CreditCardTransaction.signed_amount`: the same sign rule as a query expression,
    so register running balances compute in the database without materializing the register. MUST
    stay in lockstep with the property — a test asserts the two agree for every transaction type."""
    return models.Case(
        models.When(txn_type__in=INCREASE_TYPES, then=models.F("amount")),
        default=-models.F("amount"),
        output_field=models.DecimalField(
            max_digits=AMOUNT_MAX_DIGITS, decimal_places=AMOUNT_DECIMALS
        ),
    )
