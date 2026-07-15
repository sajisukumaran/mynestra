"""Loans & Liabilities (module 7) — a consolidated household view of debt.

A **loan** is a liability account: each `Loan` owns one postable `finance.Account` nested under the
`2200 Loans` header matching its `loan_type` (or `2900 Other Liabilities` / `2950 Contingent
Liabilities`), normal_side=credit, and every `LoanTransaction` posts a balanced journal entry via
`apps.loans.services` — disbursements/draws/interest/fees increase the balance owed, payments and
extra-principal reduce it. Balances are computed from posted lines (no stored balance); the
amortization schedule and payoff projection are pure-read overlays that post nothing (see
`apps.loans.amortization`), exactly like the investments value-over-time overlay.

Loans mirror the Cards module (a credit-normal liability subledger), with three additions: a payment
carries a component split (principal / interest / escrow / fees / extra), a funding mode (a tracked
bank account, cash, or an external/another-party payment), and borrowers-with-roles so a co-signed
loan someone else pays is fully modelled. Soft-deletable + audited like every tenant model (§5).
"""

import datetime

from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.models import SoftDeleteModel, TimeStampedModel
from apps.core.partialdate import PartialDate
from apps.finance.models import AMOUNT_DECIMALS, AMOUNT_MAX_DIGITS, ZERO


class LoanType(models.TextChoices):
    MORTGAGE = "mortgage", "Mortgage"
    AUTO = "auto", "Auto loan"
    STUDENT = "student", "Student loan"
    PERSONAL = "personal", "Personal loan"
    HELOC = "heloc", "HELOC"
    LINE_OF_CREDIT = "line_of_credit", "Line of credit"
    OTHER = "other", "Other liability"


# Which COA header each loan_type nests under (the ensure_gl_account analog of _parent_code):
LOAN_TYPE_PARENT = {
    LoanType.MORTGAGE: "2210",
    LoanType.AUTO: "2220",
    LoanType.PERSONAL: "2230",
    LoanType.STUDENT: "2240",
    LoanType.HELOC: "2250",
    LoanType.LINE_OF_CREDIT: "2260",
    LoanType.OTHER: "2900",
}
# Loans that carry a variable balance and no fixed amortization schedule.
REVOLVING_TYPES = frozenset({LoanType.HELOC, LoanType.LINE_OF_CREDIT})
# Loans that amortize on a fixed schedule.
INSTALLMENT_TYPES = frozenset(
    {LoanType.MORTGAGE, LoanType.AUTO, LoanType.STUDENT, LoanType.PERSONAL}
)
# Chip/donut tint per loan_type (all in .tint-* in app.css).
LOAN_TYPE_TINT = {
    LoanType.MORTGAGE: "blue",
    LoanType.AUTO: "amber",
    LoanType.STUDENT: "violet",
    LoanType.PERSONAL: "teal",
    LoanType.HELOC: "emerald",
    LoanType.LINE_OF_CREDIT: "sky",
    LoanType.OTHER: "slate",
}


class RateType(models.TextChoices):
    FIXED = "fixed", "Fixed"
    VARIABLE = "variable", "Variable"


class Frequency(models.TextChoices):
    MONTHLY = "monthly", "Monthly"
    SEMI_MONTHLY = "semi_monthly", "Semi-monthly"
    BI_WEEKLY = "bi_weekly", "Bi-weekly"
    WEEKLY = "weekly", "Weekly"


PERIODS_PER_YEAR = {
    Frequency.MONTHLY: 12,
    Frequency.SEMI_MONTHLY: 24,
    Frequency.BI_WEEKLY: 26,
    Frequency.WEEKLY: 52,
}


class BorrowerRole(models.TextChoices):
    PRIMARY = "primary", "Primary borrower"
    CO_BORROWER = "co_borrower", "Co-borrower"
    CO_SIGNER = "co_signer", "Co-signer"
    GUARANTOR = "guarantor", "Guarantor"


# Order borrowers primary-first for display.
BORROWER_ROLE_ORDER = {
    BorrowerRole.PRIMARY: 0,
    BorrowerRole.CO_BORROWER: 1,
    BorrowerRole.CO_SIGNER: 2,
    BorrowerRole.GUARANTOR: 3,
}


class LoanTxnType(models.TextChoices):
    # OPENING is created only by the loan setup form (opening balance owed), not the txn picker.
    OPENING = "opening", "Opening balance"
    DISBURSEMENT = "disbursement", "Disbursement"
    PAYMENT = "payment", "Payment"
    EXTRA_PRINCIPAL = "extra_principal", "Extra principal"
    DRAW = "draw", "Draw"
    INTEREST = "interest", "Interest"
    FEE = "fee", "Fee"
    ADJUSTMENT = "adjustment", "Balance adjustment"


# Transaction types that INCREASE the balance owed (a credit to the loan liability). Payments and
# extra-principal reduce it (by their principal + extra components only — interest/escrow/fees are
# expenses, not principal). Adjustments use the `increase` flag.
INCREASE_TYPES = frozenset(
    {
        LoanTxnType.OPENING,
        LoanTxnType.DISBURSEMENT,
        LoanTxnType.DRAW,
        LoanTxnType.INTEREST,
        LoanTxnType.FEE,
    }
)
# Types whose amount == the sum of the component split (checked in the DB).
SPLIT_TYPES = frozenset({LoanTxnType.PAYMENT, LoanTxnType.EXTRA_PRINCIPAL})

TXN_GLYPHS = {
    LoanTxnType.OPENING: "pin",
    LoanTxnType.DISBURSEMENT: "arrow-up",
    LoanTxnType.PAYMENT: "arrow-down",
    LoanTxnType.EXTRA_PRINCIPAL: "arrow-down",
    LoanTxnType.DRAW: "arrow-up",
    LoanTxnType.INTEREST: "coins",
    LoanTxnType.FEE: "arrow-up",
    LoanTxnType.ADJUSTMENT: "circle",
}


class Funding(models.TextChoices):
    BANK = "bank", "Bank account"
    CASH = "cash", "Cash on hand"
    EXTERNAL = "external", "External / another party"
    # A disbursement whose proceeds settle a vendor bill (Accounts Payable) rather than landing in a
    # tracked account — used by the Automobile module's financed-purchase settlement (a Payables
    # Payment posts the loan disbursement: Dr AP tagged the dealer / Cr the loan node).
    PAYABLE = "payable", "Payable (dealer bill)"


def _money(**kw):
    return models.DecimalField(
        max_digits=AMOUNT_MAX_DIGITS, decimal_places=AMOUNT_DECIMALS, **kw
    )


class Loan(SoftDeleteModel):
    """A household loan or liability. Its balance owed lives in the general ledger via `gl_account`
    (a liability node under the header matching its type); this row carries the human-facing
    metadata, terms, and the net-worth flag."""

    loan_type = models.CharField(
        max_length=16, choices=LoanType.choices, default=LoanType.PERSONAL
    )
    nickname = models.CharField(max_length=120)

    # Lender: a Person OR an Organization (at most one — a generic liability may have neither).
    lender_person = models.ForeignKey(
        "contacts.Person", on_delete=models.PROTECT, null=True, blank=True,
        related_name="loans_lent",
    )
    lender_organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.PROTECT, null=True, blank=True,
        related_name="loans_lent",
    )

    account_number = models.CharField(max_length=60, blank=True)  # your loan/account # with them
    currency = models.ForeignKey("finance.Currency", on_delete=models.PROTECT, related_name="+")

    # The dedicated postable ledger account (one per loan); created by
    # apps.loans.services.ensure_gl_account after the row is first saved.
    gl_account = models.OneToOneField(
        "finance.Account", on_delete=models.PROTECT, null=True, blank=True, related_name="loan"
    )

    # Installment terms (null for revolving / other liabilities).
    principal_original = _money(null=True, blank=True)
    annual_rate = models.DecimalField(
        max_digits=7, decimal_places=4, null=True, blank=True
    )  # APR percent (origination); current_rate honors LoanRateChange rows
    rate_type = models.CharField(
        max_length=8, choices=RateType.choices, default=RateType.FIXED
    )
    term_months = models.PositiveSmallIntegerField(null=True, blank=True)
    payment_frequency = models.CharField(
        max_length=12, choices=Frequency.choices, default=Frequency.MONTHLY
    )
    start_date = models.DateField(null=True, blank=True)
    first_payment_date = models.DateField(null=True, blank=True)
    payment_day = models.SmallIntegerField(null=True, blank=True)  # day of month (1–31)
    payment_amount = _money(null=True, blank=True)  # scheduled P+I(+escrow)
    escrow_amount = _money(default=ZERO)  # scheduled escrow portion of the payment (mortgages)

    # Revolving lines (HELOC / LOC).
    credit_limit = _money(null=True, blank=True)

    # Net-worth treatment. OFF (co-signed / guaranteed, someone else pays) nests the GL node under
    # 2950 Contingent Liabilities, which services.net_worth excludes.
    counts_toward_net_worth = models.BooleanField(default=True)

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
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(
                    lender_person__isnull=False, lender_organization__isnull=False
                ),
                name="loan_one_lender",
            ),
        ]

    def __str__(self) -> str:
        return self.nickname

    # --- type / lender helpers ---
    @property
    def display(self) -> str:
        return self.nickname

    @property
    def type_label(self) -> str:
        return self.get_loan_type_display()

    @property
    def type_tint(self) -> str:
        return LOAN_TYPE_TINT.get(self.loan_type, "slate")

    @property
    def is_revolving(self) -> bool:
        return self.loan_type in REVOLVING_TYPES

    @property
    def is_installment(self) -> bool:
        return self.loan_type in INSTALLMENT_TYPES

    @property
    def lender(self):
        return self.lender_person or self.lender_organization

    @property
    def lender_kind(self) -> str:
        return "person" if self.lender_person_id else "organization"

    @property
    def lender_name(self) -> str:
        party = self.lender
        if party is None:
            return ""
        for attr in ("display_name", "full_name", "name"):
            val = getattr(party, attr, "")
            if val:
                return val
        return str(party)

    @property
    def lender_tint(self) -> str:
        return getattr(self.lender, "avatar_tint", "slate")

    @property
    def lender_initials(self) -> str:
        return getattr(self.lender, "initials", "?")

    # --- balances (from the GL) ---
    @property
    def balance(self):
        """Current balance owed, base currency (positive = you owe). Computed from the GL."""
        if self.gl_account_id is None:
            return ZERO
        from apps.finance.services import account_balance

        return account_balance(self.gl_account)

    @property
    def native_balance(self):
        if self.gl_account_id is None:
            return None
        from apps.finance.services import account_native_balance

        return account_native_balance(self.gl_account)

    @property
    def display_balance(self):
        """Balance owed in the loan's own currency (never None; zero before posting)."""
        nb = self.native_balance
        return nb if nb is not None else ZERO

    @property
    def is_paid_off(self) -> bool:
        return self.balance <= ZERO

    @property
    def principal_paid(self):
        """How much principal has been paid down (original − current balance), clamped at 0."""
        if self.principal_original is None:
            return ZERO
        paid = self.principal_original - self.balance
        return paid if paid > ZERO else ZERO

    @property
    def paydown_pct(self):
        """Percent of the original principal paid off (0–100), or None without an original."""
        if not self.principal_original or self.principal_original <= ZERO:
            return None
        return (self.principal_paid / self.principal_original) * 100

    # --- revolving helpers ---
    @property
    def available_credit(self):
        if self.credit_limit is None:
            return None
        return self.credit_limit - self.balance

    @property
    def utilization(self):
        if not self.credit_limit or self.credit_limit <= 0:
            return None
        return (self.balance / self.credit_limit) * 100

    @property
    def utilization_tint(self) -> str:
        u = self.utilization
        if u is None:
            return ""
        if u >= 90:
            return "danger"
        if u >= 70:
            return "warning"
        return ""

    # --- rate ---
    @property
    def current_rate(self):
        """The APR in effect today: the latest LoanRateChange on/before today, else the origination
        `annual_rate`."""
        change = (
            self.rate_changes.filter(effective_date__lte=datetime.date.today())
            .order_by("-effective_date")
            .first()
        )
        return change.annual_rate if change else self.annual_rate

    # --- lifecycle dates ---
    @property
    def opened(self) -> PartialDate:
        return PartialDate.from_instance(self, "opened")

    @property
    def closed(self) -> PartialDate:
        return PartialDate.from_instance(self, "closed")

    @property
    def is_closed(self) -> bool:
        return self.closed.is_set


class LoanBorrower(TimeStampedModel):
    """A household member (or contact) on the loan, with a role. A co-signed loan lists the primary
    borrower (e.g. a child) and the co-signer; payments can be attributed to whoever made them."""

    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name="borrowers")
    person = models.ForeignKey(
        "contacts.Person", on_delete=models.PROTECT, related_name="loan_borrowings"
    )
    role = models.CharField(
        max_length=12, choices=BorrowerRole.choices, default=BorrowerRole.PRIMARY
    )

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["loan", "person"], name="loanborrower_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.person} ({self.get_role_display()})"

    @property
    def role_label(self) -> str:
        return self.get_role_display()

    @property
    def role_order(self) -> int:
        return BORROWER_ROLE_ORDER.get(self.role, 9)


class LoanRateChange(TimeStampedModel):
    """A dated interest-rate change for a variable/floating-rate loan. The rate in effect on a
    payment's date drives its interest pre-fill; `Loan.current_rate` uses the latest as of today.
    Fixed-rate loans simply have no rows."""

    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name="rate_changes")
    effective_date = models.DateField()
    annual_rate = models.DecimalField(max_digits=7, decimal_places=4)  # APR percent
    note = models.CharField(max_length=160, blank=True)

    class Meta:
        ordering = ["effective_date", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["loan", "effective_date"], name="loanratechange_unique"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.effective_date}: {self.annual_rate}%"


class LoanTransaction(SoftDeleteModel):
    """One line in a loan's register. Posts a balanced journal entry via `apps.loans.services`
    (kept in sync through `journal_entry` + `posting_version`). A payment carries the component
    split (principal / interest / escrow / fees / extra); only principal + extra reduce the loan
    balance — interest/escrow/fees are expenses (posted only when funded from a tracked source)."""

    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name="transactions")
    txn_type = models.CharField(max_length=16, choices=LoanTxnType.choices)
    date = models.DateField()
    amount = _money()  # headline figure (> 0); for a payment == the component sum

    # Component split (meaningful for payment / extra_principal; zero for single-figure types).
    principal = _money(default=ZERO)
    interest = _money(default=ZERO)
    escrow = _money(default=ZERO)
    fees = _money(default=ZERO)
    extra_principal = _money(default=ZERO)

    # Direction for a balance ADJUSTMENT (True = increase the balance owed).
    increase = models.BooleanField(null=True, blank=True)

    # Funding (payment / extra / disbursement / draw): a tracked bank account, cash on hand, or an
    # external/another-party flow (no tracked cash — see services rule A5).
    funding_source = models.CharField(
        max_length=10, choices=Funding.choices, default=Funding.EXTERNAL
    )
    funding_account = models.ForeignKey(
        "banking.BankAccount", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    cash_account = models.ForeignKey(
        "finance.Account", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    # Who made this payment (at most one) — the party dimension on the GL line (e.g. the co-signer's
    # son on an external payment).
    payer_person = models.ForeignKey(
        "contacts.Person",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="loan_txns",
    )
    payer_organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="loan_txns",
    )

    memo = models.CharField(max_length=255, blank=True)
    reference = models.CharField(max_length=60, blank=True)
    cleared = models.BooleanField(default=False)  # reconciliation-lite; never affects the GL

    journal_entry = models.ForeignKey(
        "finance.JournalEntry", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    posting_version = models.PositiveIntegerField(default=1)
    # The native funding transaction this txn created (a bank TRANSFER_OUT/IN), kept truthful there.
    bank_txn = models.ForeignKey(
        "banking.BankTransaction",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    history = HistoricalRecords()

    class Meta:
        ordering = ["-date", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(
                    payer_person__isnull=False, payer_organization__isnull=False
                ),
                name="loantransaction_one_payer",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="loantransaction_amount_positive"
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(txn_type__in=[LoanTxnType.PAYMENT, LoanTxnType.EXTRA_PRINCIPAL])
                    | models.Q(
                        amount=(
                            models.F("principal")
                            + models.F("interest")
                            + models.F("escrow")
                            + models.F("fees")
                            + models.F("extra_principal")
                        )
                    )
                ),
                name="loantransaction_payment_balances",
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
    def principal_reduction(self):
        """How much of this txn reduces the loan balance (principal + extra), for payments/extra."""
        return self.principal + self.extra_principal

    @property
    def balance_delta(self):
        """Signed effect on the balance owed (drives the register running balance; reconciles to
        account_balance(gl_account))."""
        if self.txn_type in SPLIT_TYPES:
            return -self.principal_reduction
        if self.txn_type == LoanTxnType.ADJUSTMENT:
            return self.amount if self.increase else -self.amount
        if self.txn_type in INCREASE_TYPES:
            return self.amount
        return ZERO

    @property
    def is_inflow(self) -> bool:
        """True when it increases the balance owed."""
        return self.balance_delta > ZERO

    @property
    def payer(self):
        return self.payer_person or self.payer_organization
