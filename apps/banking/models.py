"""Banking — household bank accounts (checking & savings) and their transaction register.

Banking is the first consumer of the finance general-ledger backbone (module 2). Each `BankAccount`
owns exactly one postable `finance.Account`, nested under the `1120 Checking` / `1130 Savings` group
header, so per-account balances roll up cleanly. Every `BankTransaction` posts a balanced journal
entry through `apps.finance.services` (never by writing ledger rows directly) and links back to it
via the entry's `source` GenericFK + a versioned `external_key`. Balances are computed from posted
lines — there is no stored balance. Soft-deletable + audited like every tenant model (DESIGN §5).
"""

from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.models import SoftDeleteModel, TimeStampedModel
from apps.core.partialdate import PartialDate
from apps.finance.models import AMOUNT_DECIMALS, AMOUNT_MAX_DIGITS


class AccountType(models.TextChoices):
    CHECKING = "checking", "Checking"
    SAVINGS = "savings", "Savings"


class TxnType(models.TextChoices):
    # `OPENING` is created only by the account setup form (opening balance), not the txn picker.
    OPENING = "opening", "Opening balance"
    DEPOSIT = "deposit", "Deposit"
    WITHDRAWAL = "withdrawal", "Withdrawal"
    INTEREST = "interest", "Interest"
    FEE = "fee", "Fee"
    CHARGE = "charge", "Charge"
    TRANSFER_OUT = "transfer_out", "Transfer out"
    TRANSFER_IN = "transfer_in", "Transfer in"


# Money flows INTO the account for these types, OUT for the rest (drives the signed running balance).
INFLOW_TYPES = frozenset(
    {TxnType.OPENING, TxnType.DEPOSIT, TxnType.INTEREST, TxnType.TRANSFER_IN}
)

# Lucide glyphs per transaction type — directional accents (money in ↓ / out ↑), plus a couple of
# distinct ones. All present in templates/_icon_sprite.html (the register leads with a type badge).
TXN_GLYPHS = {
    TxnType.OPENING: "pin",
    TxnType.DEPOSIT: "arrow-down",
    TxnType.WITHDRAWAL: "arrow-up",
    TxnType.INTEREST: "coins",
    TxnType.FEE: "arrow-up",
    TxnType.CHARGE: "arrow-up",
    TxnType.TRANSFER_OUT: "arrow-up",
    TxnType.TRANSFER_IN: "arrow-down",
}


class BankAccount(SoftDeleteModel):
    """A household bank account. Its balance lives in the general ledger via `gl_account`; this row
    carries the human-facing metadata (bank, holders, number, currency, lifecycle)."""

    bank = models.ForeignKey(
        "organizations.Organization", on_delete=models.PROTECT, related_name="bank_accounts"
    )
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="bank_accounts",
    )
    account_type = models.CharField(
        max_length=10, choices=AccountType.choices, default=AccountType.CHECKING
    )
    nickname = models.CharField(max_length=120)
    number = models.CharField(max_length=40, blank=True)  # account number, displayed masked
    currency = models.ForeignKey("finance.Currency", on_delete=models.PROTECT, related_name="+")

    # The dedicated postable ledger account (one per bank account); created by
    # apps.banking.services.ensure_gl_account after the row is first saved.
    gl_account = models.OneToOneField(
        "finance.Account",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="bank_account",
    )

    is_active = models.BooleanField(default=True)

    # Lifecycle (PartialDate): when the account was opened and, if it has closed, when.
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
    def type_label(self) -> str:
        return self.get_account_type_display()

    @property
    def balance(self):
        """Current base-currency balance, computed from posted ledger lines."""
        if self.gl_account_id is None:
            from apps.finance.models import ZERO

            return ZERO
        from apps.finance.services import account_balance

        return account_balance(self.gl_account)

    @property
    def native_balance(self):
        """Balance in the account's own currency (equals `balance` when that is the base currency)."""
        if self.gl_account_id is None:
            return None
        from apps.finance.services import account_native_balance

        return account_native_balance(self.gl_account)

    @property
    def opened(self) -> PartialDate:
        return PartialDate.from_instance(self, "opened")

    @property
    def closed(self) -> PartialDate:
        return PartialDate.from_instance(self, "closed")

    @property
    def is_closed(self) -> bool:
        return self.closed.is_set


class BankAccountHolder(TimeStampedModel):
    """A household member who holds a bank account. Multiple holders model a joint account; one may
    be flagged primary. Unique per (account, person)."""

    account = models.ForeignKey(BankAccount, on_delete=models.CASCADE, related_name="holders")
    person = models.ForeignKey(
        "contacts.Person", on_delete=models.PROTECT, related_name="bank_holdings"
    )
    is_primary = models.BooleanField(default=False)

    class Meta:
        ordering = ["-is_primary", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["account", "person"], name="bankaccountholder_unique"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.person} @ {self.account}"


class BankTransaction(SoftDeleteModel):
    """One line in a bank account's register. Posts a balanced journal entry via
    `apps.banking.services` (kept in sync through `journal_entry` + `posting_version`)."""

    account = models.ForeignKey(BankAccount, on_delete=models.CASCADE, related_name="transactions")
    txn_type = models.CharField(max_length=12, choices=TxnType.choices)
    date = models.DateField()
    amount = models.DecimalField(max_digits=AMOUNT_MAX_DIGITS, decimal_places=AMOUNT_DECIMALS)

    # Contra account for deposits/withdrawals (the income/expense/cash "category"). Fixed contras
    # (interest/fees/charges/transfers) are resolved by the service, so this stays null for them.
    category_account = models.ForeignKey(
        "finance.Account", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    # The other side of a transfer: a tracked account, and/or a free-text external counterpart.
    counter_account = models.ForeignKey(
        BankAccount,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="counter_transactions",
    )
    counter_external = models.CharField(max_length=160, blank=True)

    # Optional payee (at most one) — flows to the GL line's counterparty dimension.
    payee_person = models.ForeignKey(
        "contacts.Person", on_delete=models.PROTECT, null=True, blank=True, related_name="bank_txns"
    )
    payee_organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="bank_txns",
    )

    memo = models.CharField(max_length=255, blank=True)
    reference = models.CharField(max_length=60, blank=True)
    cleared = models.BooleanField(default=False)  # reconciliation-lite; never affects the GL

    # The posted ledger entry this transaction generated (also linked via the entry's source GFK).
    journal_entry = models.ForeignKey(
        "finance.JournalEntry",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    # Bumped on every reverse+repost so the versioned external_key stays unique across edits.
    posting_version = models.PositiveIntegerField(default=1)

    history = HistoricalRecords()

    class Meta:
        ordering = ["-date", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(
                    payee_person__isnull=False, payee_organization__isnull=False
                ),
                name="banktransaction_one_payee",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="banktransaction_amount_positive"
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
    def is_inflow(self) -> bool:
        return self.txn_type in INFLOW_TYPES

    @property
    def direction(self) -> str:
        return "in" if self.is_inflow else "out"

    @property
    def signed_amount(self):
        return self.amount if self.is_inflow else -self.amount

    @property
    def payee(self):
        return self.payee_person or self.payee_organization
