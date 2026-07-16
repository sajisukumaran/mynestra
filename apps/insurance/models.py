"""Insurance (Plan B) — a cross-cutting household insurance register: policies of every type
(auto / home / renters / health / life / umbrella / disability / pet / ...), their coverages,
covered persons + beneficiaries, the assets they cover, and premium payments.

A policy posts NOTHING to the general ledger by itself (like `automobile.VehicleValuation` /
`investments.SecurityPrice`). Two things touch money, both through existing service layers (never
hand-written ledger rows):

1. **Premiums → locked Payables bills (+ optional locked Payments).** An `InsurancePremium`
   materializes a `payables.Bill` (`is_locked=True`, `source=<premium>`, one line to the policy-type
   expense account) and, when funded, a locked `payables.Payment`. Mirrors the Automobile module.
2. **Claims → a direct finance entry** (Phase 2) — a payout offsetting the loss expense, with an
   auto total-loss routed into a `VehicleDisposal` (`4930`).

Soft-deletable + audited like every tenant model (§5). The premium is the money event
(SoftDeleteModel + history); the policy owns it. Coverages / members / covered-assets are current-
state child collections (TimeStampedModel, rewritten in place — the VehicleDriver idiom).
"""

import datetime

from django.contrib.contenttypes.fields import GenericForeignKey
from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.models import SoftDeleteModel, TimeStampedModel
from apps.finance.models import AMOUNT_DECIMALS, AMOUNT_MAX_DIGITS, ZERO


class PolicyType(models.TextChoices):
    AUTO = "auto", "Auto"
    HOME = "home", "Homeowners"
    RENTERS = "renters", "Renters"
    HEALTH = "health", "Health"
    DENTAL = "dental", "Dental"
    VISION = "vision", "Vision"
    LIFE = "life", "Life"
    UMBRELLA = "umbrella", "Umbrella / liability"
    DISABILITY = "disability", "Disability"
    PET = "pet", "Pet"
    OTHER = "other", "Other"


class PolicyStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    PENDING = "pending", "Pending"
    LAPSED = "lapsed", "Lapsed"
    CANCELLED = "cancelled", "Cancelled"


class PremiumFrequency(models.TextChoices):
    MONTHLY = "monthly", "Monthly"
    QUARTERLY = "quarterly", "Quarterly"
    SEMIANNUAL = "semiannual", "Semi-annual"
    ANNUAL = "annual", "Annual"
    ONE_TIME = "one_time", "One-time"


# Payments per year, for the annualized-premium stat (one-time is treated as a single annual cost).
FREQUENCY_PER_YEAR = {
    PremiumFrequency.MONTHLY: 12,
    PremiumFrequency.QUARTERLY: 4,
    PremiumFrequency.SEMIANNUAL: 2,
    PremiumFrequency.ANNUAL: 1,
    PremiumFrequency.ONE_TIME: 1,
}


class MemberRole(models.TextChoices):
    POLICYHOLDER = "policyholder", "Policyholder"
    INSURED = "insured", "Insured"
    DEPENDENT = "dependent", "Dependent"
    DRIVER = "driver", "Driver"
    BENEFICIARY = "beneficiary", "Beneficiary"


# Roles whose person is a COVERED party (→ an org-level 'insured' P2O link to the insurer).
COVERED_ROLES = frozenset(
    {MemberRole.POLICYHOLDER, MemberRole.INSURED, MemberRole.DEPENDENT, MemberRole.DRIVER}
)


class Funding(models.TextChoices):
    """How a premium was (or wasn't) paid — hand-coded so payables' own Funding enum values can't
    leak in. NONE records an accrued (unpaid) bill only."""

    BANK = "bank", "Bank account"
    CARD = "card", "Credit card"
    CASH = "cash", "Cash / other"
    NONE = "none", "Unpaid (record bill only)"


# Chip/donut tint per policy type (all in .tint-* in app.css).
POLICY_TYPE_TINT = {
    PolicyType.AUTO: "sky",
    PolicyType.HOME: "teal",
    PolicyType.RENTERS: "emerald",
    PolicyType.HEALTH: "rose",
    PolicyType.DENTAL: "sky",
    PolicyType.VISION: "violet",
    PolicyType.LIFE: "violet",
    PolicyType.UMBRELLA: "indigo",
    PolicyType.DISABILITY: "amber",
    PolicyType.PET: "orange",
    PolicyType.OTHER: "slate",
}

# Glyphs are chosen from the current icon sprite (templates/_icon_sprite.html) so nothing renders
# blank; at the UI gate the sprite can be regenerated to swap in more specific marks (umbrella,
# paw-print, accessibility, heart-pulse) if desired.
POLICY_TYPE_GLYPH = {
    PolicyType.AUTO: "car",
    PolicyType.HOME: "house",
    PolicyType.RENTERS: "key-round",
    PolicyType.HEALTH: "activity",
    PolicyType.DENTAL: "activity",
    PolicyType.VISION: "activity",
    PolicyType.LIFE: "heart",
    PolicyType.UMBRELLA: "shield-check",
    PolicyType.DISABILITY: "user-round",
    PolicyType.PET: "star",
    PolicyType.OTHER: "shield-check",
}


def _money(**kw):
    return models.DecimalField(
        max_digits=AMOUNT_MAX_DIGITS, decimal_places=AMOUNT_DECIMALS, **kw
    )


class InsurancePolicy(SoftDeleteModel):
    """One insurance policy of any type. Holds the carrier, term, nominal premium, and status; it
    posts nothing itself. Its premiums (money events) and claims (Phase 2) carry the GL effect. The
    insurer is a Person OR an Organization (at most one — the vendor on its premium bills)."""

    policy_type = models.CharField(
        max_length=12, choices=PolicyType.choices, default=PolicyType.AUTO
    )

    # Carrier: a Person OR an Organization (at most one). A policy may be drafted without an
    # insurer, but a premium can't post until one is set (the bill needs exactly one vendor).
    insurer_organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="insurance_policies",
    )
    insurer_person = models.ForeignKey(
        "contacts.Person", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="insurance_policies",
    )

    nickname = models.CharField(max_length=120, blank=True)
    plan_name = models.CharField(max_length=120, blank=True)
    policy_number = models.CharField(max_length=80, blank=True)

    status = models.CharField(
        max_length=10, choices=PolicyStatus.choices, default=PolicyStatus.ACTIVE
    )
    effective_date = models.DateField(null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True)  # renewal date (drives reminders)

    currency = models.ForeignKey(
        "finance.Currency", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    premium_amount = _money(default=ZERO)  # nominal premium per period (display + annualized stat)
    premium_frequency = models.CharField(
        max_length=12, choices=PremiumFrequency.choices, default=PremiumFrequency.ANNUAL
    )

    notes = models.TextField(blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["-id"]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(
                    insurer_person__isnull=False, insurer_organization__isnull=False
                ),
                name="insurancepolicy_one_insurer",
            ),
        ]

    def __str__(self) -> str:
        return self.display

    # --- identity / type helpers ---
    @property
    def display(self) -> str:
        if self.nickname:
            return self.nickname
        base = self.get_policy_type_display()
        return f"{base} · {self.insurer_name}" if self.insurer else base

    @property
    def type_label(self) -> str:
        return self.get_policy_type_display()

    @property
    def type_tint(self) -> str:
        return POLICY_TYPE_TINT.get(self.policy_type, "slate")

    @property
    def type_glyph(self) -> str:
        return POLICY_TYPE_GLYPH.get(self.policy_type, "shield")

    @property
    def status_label(self) -> str:
        return self.get_status_display()

    @property
    def is_active(self) -> bool:
        return self.status == PolicyStatus.ACTIVE

    # --- insurer party ---
    @property
    def insurer(self):
        return self.insurer_person or self.insurer_organization

    @property
    def insurer_kind(self) -> str:
        return "person" if self.insurer_person_id else "organization"

    @property
    def insurer_name(self) -> str:
        party = self.insurer
        if party is None:
            return ""
        for attr in ("display_name", "full_name", "display", "name"):
            val = getattr(party, attr, "")
            if val:
                return val
        return str(party)

    # --- premium / term helpers ---
    @property
    def annualized_premium(self):
        return (self.premium_amount or ZERO) * FREQUENCY_PER_YEAR.get(self.premium_frequency, 1)

    @property
    def frequency_label(self) -> str:
        return self.get_premium_frequency_display()

    @property
    def days_until_expiry(self):
        if not self.expiry_date:
            return None
        return (self.expiry_date - datetime.date.today()).days

    @property
    def is_expired(self) -> bool:
        return bool(self.expiry_date and self.expiry_date < datetime.date.today())


class PolicyCoverage(TimeStampedModel):
    """A structured coverage line on a policy (e.g. auto: liability / collision / comprehensive;
    home: dwelling / personal-property / liability), with its limit + deductible."""

    policy = models.ForeignKey(
        InsurancePolicy, on_delete=models.CASCADE, related_name="coverages"
    )
    coverage_type = models.CharField(max_length=80)
    limit_amount = _money(null=True, blank=True)
    deductible_amount = _money(null=True, blank=True)
    premium_portion = _money(null=True, blank=True)
    note = models.CharField(max_length=200, blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self) -> str:
        return self.coverage_type


class PolicyMember(TimeStampedModel):
    """A person on a policy, with a role. Covered parties (policyholder / insured / dependent /
    driver) get an org-level 'insured' P2O link to the insurer; beneficiaries (life) carry an
    optional percentage. One row per (policy, person, role)."""

    policy = models.ForeignKey(
        InsurancePolicy, on_delete=models.CASCADE, related_name="members"
    )
    person = models.ForeignKey(
        "contacts.Person", on_delete=models.PROTECT, related_name="insurance_memberships"
    )
    role = models.CharField(max_length=12, choices=MemberRole.choices, default=MemberRole.INSURED)
    beneficiary_percent = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True
    )
    relationship_note = models.CharField(max_length=120, blank=True)
    covered_from = models.DateField(null=True, blank=True)
    covered_to = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["policy", "person", "role"], name="policymember_unique"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.person} ({self.get_role_display()})"

    @property
    def role_label(self) -> str:
        return self.get_role_display()

    @property
    def is_beneficiary(self) -> bool:
        return self.role == MemberRole.BENEFICIARY


class PolicyAsset(TimeStampedModel):
    """An asset a policy covers, via a generic FK so the covered-asset type can vary by module (auto
    → `automobile.Vehicle` now; home → the Real Estate module's Property in Plan C). One row per
    (policy, asset)."""

    policy = models.ForeignKey(
        InsurancePolicy, on_delete=models.CASCADE, related_name="assets"
    )
    content_type = models.ForeignKey(
        "contenttypes.ContentType", on_delete=models.CASCADE, related_name="+"
    )
    object_id = models.PositiveBigIntegerField()
    covered_asset = GenericForeignKey("content_type", "object_id")
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["policy", "content_type", "object_id"], name="policyasset_unique"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.policy_id} covers {self.content_type_id}:{self.object_id}"

    @property
    def asset_label(self) -> str:
        asset = self.covered_asset
        if asset is None:
            return "—"
        for attr in ("full_name", "nickname", "display_name", "display", "name"):
            val = getattr(asset, attr, "")
            if val:
                return val
        return str(asset)


class InsurancePremium(SoftDeleteModel):
    """A premium payment on a policy. Its GL effect lives entirely on a linked locked
    `payables.Bill` (and, when funded, a locked `payables.Payment`) — this row carries none of its
    own journal entry. The vendor on the bill is the policy's insurer (one party required to post).
    """

    policy = models.ForeignKey(
        InsurancePolicy, on_delete=models.CASCADE, related_name="premiums"
    )
    date = models.DateField()
    amount = _money()  # > 0

    covers_from = models.DateField(null=True, blank=True)
    # The period end this premium covers — advances the policy's expiry/renewal date.
    covers_through = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)  # → bill.due_date (payables aging)

    # The locked payables document(s) this premium owns (direct links; the payables source GFK is
    # unindexed). `payment` is the module-created funding payment, kept for teardown.
    bill = models.OneToOneField(
        "payables.Bill", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="insurance_premium",
    )
    payment = models.ForeignKey(
        "payables.Payment", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    funding_source = models.CharField(
        max_length=8, choices=Funding.choices, default=Funding.NONE
    )
    funding_account = models.ForeignKey(
        "banking.BankAccount", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    credit_card = models.ForeignKey(
        "cards.CreditCard", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    cash_account = models.ForeignKey(
        "finance.Account", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    reference = models.CharField(max_length=80, blank=True)  # → bill.vendor_ref
    memo = models.CharField(max_length=255, blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["-date", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="insurancepremium_amount_positive"
            ),
        ]

    def __str__(self) -> str:
        return f"Premium {self.amount} on {self.date}"

    @property
    def is_funded(self) -> bool:
        return self.funding_source in (Funding.BANK, Funding.CARD, Funding.CASH)

    # Duck-typed hooks read by the Payables locked-bill/payment back-link (module-agnostic there):
    # the owning module's human label + the tenant-relative path back to the record.
    @property
    def managed_label(self) -> str:
        return f"Insurance · {self.policy.display}"

    @property
    def managed_url(self) -> str:
        return f"insurance/policies/{self.policy_id}/"


class ClaimStatus(models.TextChoices):
    OPEN = "open", "Open"
    SUBMITTED = "submitted", "Submitted"
    APPROVED = "approved", "Approved"
    PAID = "paid", "Paid"
    DENIED = "denied", "Denied"
    CLOSED = "closed", "Closed"


# Statuses that count as "in progress" for the open-claims stat / launcher count.
OPEN_CLAIM_STATUSES = frozenset(
    {ClaimStatus.OPEN, ClaimStatus.SUBMITTED, ClaimStatus.APPROVED}
)


class SettlementKind(models.TextChoices):
    REIMBURSEMENT = "reimbursement", "Reimbursement"
    TOTAL_LOSS = "total_loss", "Total loss (auto write-off)"


class PayoutDestination(models.TextChoices):
    """Where a claim payout lands. No `card` (a payout is money IN, not a card charge); NONE = no
    payout received yet (an open claim posts nothing on the reimbursement path)."""

    BANK = "bank", "Bank account"
    CASH = "cash", "Cash / other"
    NONE = "none", "No payout yet"


class Claim(SoftDeleteModel):
    """A claim on a policy — the money event for a covered loss. It posts through one of two paths,
    never hand-written ledger rows (mirrors `InsurancePremium` / `VehicleDisposal`):

    * **Reimbursement** — a direct finance entry crediting the loss expense account (the payout
      offsets the already-booked loss; net retained expense = the deductible). Payout to a tracked
      bank routes via `1150` + a native banking TRANSFER_IN so the register stays truthful; to cash
      it debits the chosen cash account (else `1110`). Posts only once a payout is recorded.
    * **Total loss (auto)** — delegates entirely to `automobile.post_disposal`
      (`DisposalMethod.TOTAL_LOSS`, `proceeds=payout`), booking gain/loss to `4930`; this row posts
      NOTHING of its own (the disposal owns the entry — double-book guard).
    """

    policy = models.ForeignKey(InsurancePolicy, on_delete=models.CASCADE, related_name="claims")
    claim_number = models.CharField(max_length=80, blank=True)
    loss_date = models.DateField()
    reported_date = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=10, choices=ClaimStatus.choices, default=ClaimStatus.OPEN
    )
    settlement_kind = models.CharField(
        max_length=14, choices=SettlementKind.choices, default=SettlementKind.REIMBURSEMENT
    )

    deductible_amount = _money(default=ZERO)  # informational: retained expense after the payout
    payout_amount = _money(default=ZERO)      # >= 0; 0 = no payout yet / denied
    payout_date = models.DateField(null=True, blank=True)

    payout_destination = models.CharField(
        max_length=6, choices=PayoutDestination.choices, default=PayoutDestination.NONE
    )
    bank_account = models.ForeignKey(
        "banking.BankAccount", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    cash_account = models.ForeignKey(
        "finance.Account", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    # Reimbursement: the expense account the payout credits (the loss's own expense home).
    loss_expense_account = models.ForeignKey(
        "finance.Account", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    # The covered asset this claim concerns (GFK, mirrors PolicyAsset). For a total loss it resolves
    # the Vehicle to dispose; otherwise it's display-only. Real-estate-ready.
    content_type = models.ForeignKey(
        "contenttypes.ContentType", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    object_id = models.PositiveBigIntegerField(null=True, blank=True)
    claimed_asset = GenericForeignKey("content_type", "object_id")

    # Posting handles (exactly one path is used).
    journal_entry = models.ForeignKey(
        "finance.JournalEntry", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    bank_txn = models.ForeignKey(
        "banking.BankTransaction", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    disposal = models.ForeignKey(
        "automobile.VehicleDisposal", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    posting_version = models.PositiveIntegerField(default=1)  # bumped on edit (reverse + rebuild)
    notes = models.TextField(blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["-loss_date", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(payout_amount__gte=0), name="claim_payout_nonnegative"
            ),
            models.CheckConstraint(
                condition=~models.Q(bank_account__isnull=False, cash_account__isnull=False),
                name="claim_one_destination_account",
            ),
        ]

    def __str__(self) -> str:
        return f"Claim {self.claim_number or self.pk} on {self.policy_id}"

    # --- labels / helpers ---
    @property
    def status_label(self) -> str:
        return self.get_status_display()

    @property
    def settlement_label(self) -> str:
        return self.get_settlement_kind_display()

    @property
    def is_total_loss(self) -> bool:
        return self.settlement_kind == SettlementKind.TOTAL_LOSS

    @property
    def has_payout(self) -> bool:
        return bool(self.payout_amount and self.payout_amount > ZERO)

    @property
    def asset_label(self) -> str:
        asset = self.claimed_asset
        if asset is None:
            return ""
        for attr in ("full_name", "nickname", "display_name", "display", "name"):
            val = getattr(asset, attr, "")
            if val:
                return val
        return str(asset)

    @property
    def gain_loss(self):
        """For a posted total-loss claim, the booked gain/loss read through its disposal; otherwise
        None (a reimbursement doesn't book a gain/loss)."""
        if self.disposal_id is not None:
            return self.disposal.gain_loss
        return None


class DocumentType(models.TextChoices):
    POLICY_CONTRACT = "policy_contract", "Policy contract"
    DECLARATION = "declaration_page", "Declaration page"
    ID_CARD = "id_card", "ID card"
    RECEIPT = "receipt", "Receipt"
    CORRESPONDENCE = "correspondence", "Correspondence"
    CLAIM = "claim", "Claim document"
    OTHER = "other", "Other"


class PolicyDocument(TimeStampedModel):
    """An uploaded file attached to a policy (contract, declaration page, ID card, claim
    correspondence, ...). No GL effect — a plain attachment, optionally scoped to a claim. Files
    share MEDIA_ROOT with the rest of the app (vehicle_docs / person_photos); the row is schema-
    isolated like every tenant record."""

    policy = models.ForeignKey(
        InsurancePolicy, on_delete=models.CASCADE, related_name="documents"
    )
    claim = models.ForeignKey(
        Claim, on_delete=models.SET_NULL, null=True, blank=True, related_name="documents"
    )
    title = models.CharField(max_length=160)
    doc_type = models.CharField(
        max_length=16, choices=DocumentType.choices, default=DocumentType.OTHER
    )
    file = models.FileField(upload_to="insurance_docs/")
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return self.title

    @property
    def type_label(self) -> str:
        return self.get_doc_type_display()

    @property
    def filename(self) -> str:
        import os

        return os.path.basename(self.file.name) if self.file else ""
