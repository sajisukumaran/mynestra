"""Health (Plan D) — a cross-cutting medical accounts-payable / invoice-reconciliation register:
visits (encounters), the provider invoices billed per episode, their optional full-EOB service
lines, partial payments, and attached documents.

Modeled on `apps/insurance`: it posts **nothing** to the general ledger directly and is **not a
balance-sheet asset** (no `1xxx.NN` node, no disposal, no `4930`). Everything that touches money
moves through the **Payables backbone**, never hand-written ledger rows:

* **Each provider invoice materializes a locked (read-only) `payables.Bill`** (`is_locked=True`,
  `source=<invoice>`, one expense line per EOB charge — or a single `amount_due` line — to the
  encounter-type expense account under the `5400` header). The Payables UI 403s any edit; the
  invoice's lifecycle is owned here (`post_invoice_bill` / in-place `repost_bill` / `unpost_bill`).
* **Payments are locked `payables.Payment`s** (bank / card / cash / **HSA**), sourced the same way;
  a bill can carry **multiple** allocations (partial payments / plans). PAID / PARTIALLY_PAID
  derive from the allocation totals, never the GL.

The visit + each invoice are money-event records (SoftDeleteModel + history); the provider roster
and EOB service lines are current-state child collections (TimeStampedModel, rewritten in place).
"""

import datetime
import os

from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.models import SoftDeleteModel, TimeStampedModel
from apps.finance.models import AMOUNT_DECIMALS, AMOUNT_MAX_DIGITS, ZERO


def _money(**kw):
    return models.DecimalField(
        max_digits=AMOUNT_MAX_DIGITS, decimal_places=AMOUNT_DECIMALS, **kw
    )


# --- Enums -----------------------------------------------------------------------------------

class EncounterType(models.TextChoices):
    MEDICAL = "medical", "Medical"
    DENTAL = "dental", "Dental"
    VISION = "vision", "Vision"
    HOSPITAL = "hospital", "Hospital"


class EncounterSetting(models.TextChoices):
    OFFICE = "office", "Doctor's office"
    SPECIALIST = "specialist", "Specialist"
    URGENT_CARE = "urgent_care", "Urgent care"
    ER = "er", "Emergency room"
    INPATIENT = "inpatient", "Inpatient / hospital stay"
    OUTPATIENT = "outpatient", "Outpatient"
    LAB = "lab", "Lab"
    IMAGING = "imaging", "Imaging / radiology"
    TELEHEALTH = "telehealth", "Telehealth"


class VisitStatus(models.TextChoices):
    SCHEDULED = "scheduled", "Scheduled"
    COMPLETED = "completed", "Completed"
    CANCELLED = "cancelled", "Cancelled"


class ProviderRole(models.TextChoices):
    ATTENDING = "attending", "Attending / primary"
    SPECIALIST = "specialist", "Specialist"
    SURGEON = "surgeon", "Surgeon"
    NURSE = "nurse", "Nurse"
    TECHNICIAN = "technician", "Technician"
    REFERRING = "referring", "Referring"
    ANESTHESIOLOGIST = "anesthesiologist", "Anesthesiologist"
    OTHER = "other", "Other"


class InvoiceStatus(models.TextChoices):
    """The 7-state invoice lifecycle (§lifecycle). UNPAID/PARTIALLY_PAID/PAID/OVERPAID derive from
    the bill's allocations; PENDING_INSURANCE/DISPUTED/WRITTEN_OFF are sticky manual states."""

    PENDING_INSURANCE = "pending_insurance", "Pending insurance"
    UNPAID = "unpaid", "Unpaid"
    PARTIALLY_PAID = "partially_paid", "Partially paid"
    PAID = "paid", "Paid"
    DISPUTED = "disputed", "Disputed"
    WRITTEN_OFF = "written_off", "Written off / adjusted"
    OVERPAID = "overpaid", "Overpaid (refund due)"


# Statuses whose bill is posted (accrued). Pending-insurance posts nothing until the amount is
# confirmed; everything else carries a bill.
POSTED_STATUSES = frozenset({
    InvoiceStatus.UNPAID, InvoiceStatus.PARTIALLY_PAID, InvoiceStatus.PAID,
    InvoiceStatus.DISPUTED, InvoiceStatus.WRITTEN_OFF, InvoiceStatus.OVERPAID,
})

# Statuses that count as "you owe now" (drive the you-owe total + overdue nags). Disputed is set
# aside; written-off / paid / overpaid are settled; pending-insurance isn't on the books yet.
OWED_STATUSES = frozenset({InvoiceStatus.UNPAID, InvoiceStatus.PARTIALLY_PAID})


class Funding(models.TextChoices):
    """How an invoice payment was funded — hand-coded so payables' own Funding values can't leak in.
    HSA is a first-class source (settles AP straight from a health-savings account)."""

    BANK = "bank", "Bank account"
    CARD = "card", "Credit card"
    CASH = "cash", "Cash / other"
    HSA = "hsa", "HSA"


class DocumentType(models.TextChoices):
    EOB = "eob", "Explanation of benefits"
    ITEMIZED_BILL = "itemized_bill", "Itemized bill"
    STATEMENT = "statement", "Statement"
    RECEIPT = "receipt", "Receipt"
    INSURANCE_CARD = "insurance_card", "Insurance card"
    LAB_RESULT = "lab_result", "Lab result"
    IMAGING = "imaging", "Imaging"
    REFERRAL = "referral", "Referral"
    PRESCRIPTION = "prescription", "Prescription"
    OTHER = "other", "Other"


# EncounterType → the Standard-mode default expense account (a stable system_key under 5400). All
# remappable per encounter in Expert mode via a PostingMap.
ENCOUNTER_TYPE_ACCOUNT = {
    EncounterType.MEDICAL: "medical_expense",    # 5410
    EncounterType.DENTAL: "dental_expense",      # 5420
    EncounterType.VISION: "vision_expense",      # 5430
    EncounterType.HOSPITAL: "hospital_expense",  # 5450
}

# Chip/tile tint per encounter type (all in .tint-* / .tile-glyph.* in app.css).
ENCOUNTER_TYPE_TINT = {
    EncounterType.MEDICAL: "rose",
    EncounterType.DENTAL: "sky",
    EncounterType.VISION: "violet",
    EncounterType.HOSPITAL: "teal",
}

# Glyphs from the current icon sprite (templates/_icon_sprite.html) so nothing renders blank; at the
# UI gate the sprite can be regenerated to swap in more specific marks (stethoscope / tooth / eye).
ENCOUNTER_TYPE_GLYPH = {
    EncounterType.MEDICAL: "activity",
    EncounterType.DENTAL: "activity",
    EncounterType.VISION: "activity",
    EncounterType.HOSPITAL: "landmark",
}


# --- Encounter (the visit / episode) ---------------------------------------------------------

class Encounter(SoftDeleteModel):
    """A visit / episode — the primary record and the grouping. It groups one or more provider
    invoices (facility, physician, radiology, lab…) but **owns no bill itself**; the money lives on
    each `ProviderInvoice`. Records both the `facility` (where it happened) and the
    `primary_provider` (the main doctor seen), plus a roster of everyone involved."""

    patient = models.ForeignKey(
        "contacts.Person", on_delete=models.PROTECT, related_name="health_encounters"
    )
    encounter_type = models.CharField(
        max_length=10, choices=EncounterType.choices, default=EncounterType.MEDICAL
    )
    setting = models.CharField(
        max_length=12, choices=EncounterSetting.choices, default=EncounterSetting.OFFICE
    )
    visit_status = models.CharField(
        max_length=10, choices=VisitStatus.choices, default=VisitStatus.COMPLETED
    )
    date = models.DateField()

    # Where the visit happened (org) and the main doctor seen (person) — both allowed together (no
    # one-of constraint): you can record a facility AND a primary provider for the same visit.
    facility = models.ForeignKey(
        "organizations.Organization", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="health_encounters",
    )
    primary_provider = models.ForeignKey(
        "contacts.Person", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="health_encounters_led",
    )

    # The insurance plan in effect (set / auto-linked in P2; keeps the policy generic).
    plan = models.ForeignKey(
        "insurance.InsurancePolicy", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="health_encounters",
    )

    reason = models.CharField(max_length=200, blank=True)
    notes = models.TextField(blank=True)

    # Denormalized rollups over this encounter's invoices — recomputed whenever an invoice/payment
    # changes (a pure read cache; the invoices are the source of truth).
    total_billed = _money(default=ZERO)
    total_patient_responsibility = _money(default=ZERO)
    total_paid = _money(default=ZERO)
    total_outstanding = _money(default=ZERO)

    history = HistoricalRecords()

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self) -> str:
        return self.display

    # --- labels / helpers ---
    @property
    def display(self) -> str:
        base = self.get_encounter_type_display()
        where = self.facility.name if self.facility_id else self.setting_label
        return f"{base} · {where}" if where else base

    @property
    def type_label(self) -> str:
        return self.get_encounter_type_display()

    @property
    def type_tint(self) -> str:
        return ENCOUNTER_TYPE_TINT.get(self.encounter_type, "slate")

    @property
    def type_glyph(self) -> str:
        return ENCOUNTER_TYPE_GLYPH.get(self.encounter_type, "activity")

    @property
    def setting_label(self) -> str:
        return self.get_setting_display()

    @property
    def status_label(self) -> str:
        return self.get_visit_status_display()

    @property
    def is_scheduled(self) -> bool:
        return self.visit_status == VisitStatus.SCHEDULED

    @property
    def patient_name(self) -> str:
        return _party_name(self.patient)


# --- Encounter provider roster ---------------------------------------------------------------

class EncounterProvider(TimeStampedModel):
    """The roster of everyone involved in a visit (rewritten in place — the VehicleDriver idiom). On
    save, the module optionally persists a `provider_affiliation` P2O link (person ↔ org)."""

    encounter = models.ForeignKey(
        Encounter, on_delete=models.CASCADE, related_name="providers"
    )
    person = models.ForeignKey(
        "contacts.Person", on_delete=models.PROTECT, related_name="health_roles"
    )
    role = models.CharField(
        max_length=16, choices=ProviderRole.choices, default=ProviderRole.ATTENDING
    )
    # That person's affiliation for this visit (their practice / hospital); optional.
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="health_provider_roles",
    )
    note = models.CharField(max_length=200, blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["encounter", "person", "role"], name="encounterprovider_unique"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.person} ({self.get_role_display()})"

    @property
    def role_label(self) -> str:
        return self.get_role_display()

    @property
    def person_name(self) -> str:
        return _party_name(self.person)


# --- Provider invoice (the money event) ------------------------------------------------------

class ProviderInvoice(SoftDeleteModel):
    """One biller's statement — the money event. Owns one locked `payables.Bill` and many locked
    payments. Its biller (a business OR a person) is independent of the doctors seen on the visit,
    with an optional `rendering_provider` noting whose service it covers."""

    encounter = models.ForeignKey(
        Encounter, on_delete=models.SET_NULL, null=True, blank=True, related_name="invoices"
    )

    # Biller party: a Person OR an Organization (at most one) — the billing entity, the bill vendor.
    # Independent of the doctors seen. May be absent while Pending-insurance (posts nothing).
    biller_person = models.ForeignKey(
        "contacts.Person", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="health_invoices",
    )
    biller_organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="health_invoices",
    )
    # The doctor whose service this invoice covers (distinct from the biller).
    rendering_provider = models.ForeignKey(
        "contacts.Person", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="health_rendered_invoices",
    )

    invoice_number = models.CharField(max_length=80, blank=True)  # the provider's statement number
    invoice_date = models.DateField()
    due_date = models.DateField(null=True, blank=True)  # → bill.due_date (payables aging)
    status = models.CharField(
        max_length=18, choices=InvoiceStatus.choices, default=InvoiceStatus.UNPAID
    )

    # The bare total when not itemized; when EOB charges are present it equals Σ their
    # patient_responsibility (recomputed on save).
    amount_due = _money(default=ZERO)

    # The locked payables bill backing this invoice (posted on leaving Pending-insurance). Payments
    # are locked payables.Payments discovered via the source GFK, so no direct m2m here.
    bill = models.OneToOneField(
        "payables.Bill", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="health_invoice",
    )

    # Overpaid → the biller owes you this back; cleared by record_refund. `refunded` accumulates the
    # refunds already received, so `refund_expected` = max(0, allocated − amount_due − refunded).
    refund_expected = _money(default=ZERO)
    refunded = _money(default=ZERO)

    # Last funding choice used for a payment on this invoice (prefills the payment modal). The
    # authoritative funding lives on each locked Payment; these are convenience defaults.
    funding_source = models.CharField(max_length=8, choices=Funding.choices, blank=True)
    funding_account = models.ForeignKey(
        "banking.BankAccount", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    credit_card = models.ForeignKey(
        "cards.CreditCard", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    cash_account = models.ForeignKey(
        "finance.Account", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    hsa_account = models.ForeignKey(
        "investments.InvestmentAccount", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )

    reference = models.CharField(max_length=80, blank=True)  # → bill.vendor_ref
    memo = models.CharField(max_length=255, blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["-invoice_date", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(
                    biller_person__isnull=False, biller_organization__isnull=False
                ),
                name="providerinvoice_one_biller",
            ),
            models.CheckConstraint(
                condition=models.Q(amount_due__gte=0), name="providerinvoice_amount_nonneg"
            ),
        ]

    def __str__(self) -> str:
        return f"Invoice {self.invoice_number or self.pk} — {self.biller_name}"

    # --- biller party ---
    @property
    def biller(self):
        return self.biller_person or self.biller_organization

    @property
    def biller_kind(self) -> str:
        return "person" if self.biller_person_id else "organization"

    @property
    def biller_name(self) -> str:
        return _party_name(self.biller)

    @property
    def biller_tint(self) -> str:
        return getattr(self.biller, "avatar_tint", "slate")

    @property
    def biller_initials(self) -> str:
        return getattr(self.biller, "initials", "?")

    # --- status / money helpers ---
    @property
    def status_label(self) -> str:
        return self.get_status_display()

    @property
    def is_pending(self) -> bool:
        return self.status == InvoiceStatus.PENDING_INSURANCE

    @property
    def is_disputed(self) -> bool:
        return self.status == InvoiceStatus.DISPUTED

    @property
    def is_written_off(self) -> bool:
        return self.status == InvoiceStatus.WRITTEN_OFF

    @property
    def is_overpaid(self) -> bool:
        return self.status == InvoiceStatus.OVERPAID

    @property
    def is_settled(self) -> bool:
        return self.status in (InvoiceStatus.PAID, InvoiceStatus.WRITTEN_OFF)

    @property
    def counts_as_owed(self) -> bool:
        return self.status in OWED_STATUSES

    @property
    def amount_paid(self):
        """Total allocated against this invoice's locked bill (0 if unbilled)."""
        if self.bill_id is None:
            return ZERO
        return self.bill.amount_paid

    @property
    def outstanding(self):
        """What you still owe on this invoice (never negative; overpayment shows as a refund)."""
        bal = self.amount_due - self.amount_paid
        return bal if bal > ZERO else ZERO

    @property
    def is_overdue(self) -> bool:
        return bool(
            self.due_date
            and self.status in OWED_STATUSES
            and self.due_date < datetime.date.today()
        )

    # Duck-typed hooks read by the Payables locked-bill/payment back-link (module-agnostic there).
    @property
    def managed_label(self) -> str:
        return f"Health · {self.biller_name}"

    @property
    def managed_url(self) -> str:
        return f"health/invoices/{self.pk}/"


class InvoiceCharge(TimeStampedModel):
    """A full-EOB service line on an invoice (optional). When present the locked bill has one
    expense line per charge (Σ → the encounter-type account); when absent, one line for the
    `amount_due`. The insurance amounts (allowed / insurance_paid) are an overlay — they post
    nothing; only the patient's responsibility hits the GL (on the bill)."""

    invoice = models.ForeignKey(
        ProviderInvoice, on_delete=models.CASCADE, related_name="charges"
    )
    description = models.CharField(max_length=200)
    service_code = models.CharField(max_length=40, blank=True)  # CPT / HCPCS / ADA code

    billed = _money(default=ZERO)          # provider's charge
    allowed = _money(default=ZERO)         # plan-allowed (negotiated) amount
    insurance_paid = _money(default=ZERO)  # what the plan paid (overlay — posts nothing)

    # The patient's share breakdown (feeds deductible/OOP tracking in P2).
    deductible_amount = _money(default=ZERO)
    copay_amount = _money(default=ZERO)
    coinsurance_amount = _money(default=ZERO)
    noncovered_amount = _money(default=ZERO)

    applies_to_deductible = models.BooleanField(default=True)
    applies_to_oop = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self) -> str:
        return self.description

    @property
    def patient_responsibility(self):
        """What the patient owes for this line — the sum of the responsibility components. This is
        what the locked bill's expense line for this charge posts."""
        return (
            (self.deductible_amount or ZERO)
            + (self.copay_amount or ZERO)
            + (self.coinsurance_amount or ZERO)
            + (self.noncovered_amount or ZERO)
        )


class HealthDocument(TimeStampedModel):
    """An uploaded file attached to exactly one Health owner (an encounter, an invoice, or a
    person). No GL effect — a plain attachment; the row is schema-isolated like every tenant record.
    (P2 adds a `plan` owner; P3 a `prescription` owner — each extends the exactly-one CHECK.)"""

    encounter = models.ForeignKey(
        Encounter, on_delete=models.CASCADE, null=True, blank=True, related_name="documents"
    )
    invoice = models.ForeignKey(
        ProviderInvoice, on_delete=models.CASCADE, null=True, blank=True, related_name="documents"
    )
    person = models.ForeignKey(
        "contacts.Person", on_delete=models.CASCADE, null=True, blank=True,
        related_name="health_documents",
    )
    title = models.CharField(max_length=160)
    doc_type = models.CharField(
        max_length=16, choices=DocumentType.choices, default=DocumentType.OTHER
    )
    file = models.FileField(upload_to="health_docs/")
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            # Exactly one owner set (encounter XOR invoice XOR person).
            models.CheckConstraint(
                condition=(
                    models.Q(
                        encounter__isnull=False, invoice__isnull=True, person__isnull=True
                    )
                    | models.Q(
                        encounter__isnull=True, invoice__isnull=False, person__isnull=True
                    )
                    | models.Q(
                        encounter__isnull=True, invoice__isnull=True, person__isnull=False
                    )
                ),
                name="healthdocument_one_owner",
            ),
        ]

    def __str__(self) -> str:
        return self.title

    @property
    def type_label(self) -> str:
        return self.get_doc_type_display()

    @property
    def filename(self) -> str:
        return os.path.basename(self.file.name) if self.file else ""


class HealthPlan(TimeStampedModel):
    """Cost-sharing satellite for a medical / dental / vision insurance policy (Plan D, P2) — a
    OneToOne on `insurance.InsurancePolicy`, keeping the policy generic. Carries the plan-year
    window and the deductible / out-of-pocket / coinsurance / dental-max / vision-allowance figures
    that drive the deductible & OOP accumulator meters. Posts nothing (a pure overlay)."""

    policy = models.OneToOneField(
        "insurance.InsurancePolicy", on_delete=models.CASCADE, related_name="health_plan"
    )
    # Plan-year window (benefit year). Defaults to the calendar year (Jan 1).
    plan_year_start_month = models.PositiveSmallIntegerField(default=1)
    plan_year_start_day = models.PositiveSmallIntegerField(default=1)

    network = models.CharField(max_length=80, blank=True)  # e.g. PPO / HMO / EPO

    deductible_individual = _money(default=ZERO)
    deductible_family = _money(default=ZERO)
    oop_max_individual = _money(default=ZERO)
    oop_max_family = _money(default=ZERO)
    coinsurance_pct = models.DecimalField(  # the % the patient pays after the deductible
        max_digits=5, decimal_places=2, default=ZERO
    )

    dental_annual_max = _money(null=True, blank=True)  # a benefit cap (Σ insurance_paid vs this)
    vision_allowance = _money(null=True, blank=True)

    class Meta:
        ordering = ["-id"]

    def __str__(self) -> str:
        return f"Health plan · {self.policy.display}"

    def plan_year_window(self, as_of: datetime.date | None = None):
        """The [start, end] dates of the plan year containing `as_of` (default today)."""
        import calendar

        as_of = as_of or datetime.date.today()
        m = self.plan_year_start_month or 1
        d = self.plan_year_start_day or 1

        def _clamp(year, month, day):
            return datetime.date(year, month, min(day, calendar.monthrange(year, month)[1]))

        start_this = _clamp(as_of.year, m, d)
        start = start_this if as_of >= start_this else _clamp(as_of.year - 1, m, d)
        end = _clamp(start.year + 1, m, d) - datetime.timedelta(days=1)
        return start, end


class CopayRule(TimeStampedModel):
    """A per-service-type copay on a health plan (e.g. office visit $30, specialist $50, ER $250).
    A related child collection, rewritten in place. One row per (plan, service_type)."""

    plan = models.ForeignKey(HealthPlan, on_delete=models.CASCADE, related_name="copay_rules")
    service_type = models.CharField(max_length=80)  # free text: office / specialist / ER / …
    copay_amount = _money(default=ZERO)
    note = models.CharField(max_length=200, blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]
        constraints = [
            models.UniqueConstraint(fields=["plan", "service_type"], name="copayrule_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.service_type}: {self.copay_amount}"


def _party_name(party) -> str:
    """Best human name for a Person / Organization (or '' when unset)."""
    if party is None:
        return ""
    for attr in ("display_name", "full_name", "name"):
        val = getattr(party, attr, "")
        if val:
            return val
    return str(party)
