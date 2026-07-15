"""Automobile (module 8) — a household vehicle register that puts owned cars on the balance sheet
at cost, tracks leased cars off-balance-sheet, and routes every money event through the existing
Payables module as a READ-ONLY (locked) bill.

Mirrors the Loans module structure. An **owned** vehicle owns one postable `finance.Account` nested
under the `1420 Vehicles` header (held at cost); a **leased** vehicle has no GL node (its payments
are expenses, its refundable deposit an asset at `1320`). Every running cost — fuel, service,
insurance, registration, lease payments — is a `VehicleCostEvent` that materializes a locked
`payables.Bill` (and, when funded, a locked `payables.Payment`); the dealer purchase is a locked,
capitalizing bill. A full **disposal** is a direct finance entry (sale / trade-in / total-loss /
gift / scrap / lease-return) booking a single gain/loss to `4930`.

The GL effect of a cost event lives entirely on its linked Bill/Payment (no `journal_entry` on the
event); only a disposal posts a journal entry directly. Value-over-time is a manual dated overlay
(`VehicleValuation`, like investments' `SecurityPrice`) that posts nothing — net worth stays at cost
in v1. Soft-deletable + audited like every tenant model (§5).
"""

import datetime
from decimal import Decimal

from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.models import SoftDeleteModel, TimeStampedModel
from apps.core.partialdate import PartialDate
from apps.finance.models import AMOUNT_DECIMALS, AMOUNT_MAX_DIGITS, ZERO


class FuelType(models.TextChoices):
    GASOLINE = "gasoline", "Gasoline"
    DIESEL = "diesel", "Diesel"
    HYBRID = "hybrid", "Hybrid"
    PHEV = "phev", "Plug-in hybrid"
    EV = "ev", "Electric"
    OTHER = "other", "Other"


# Fuel types that draw (some or all) energy from the wall — the odometer/economy tab shows kWh.
ELECTRIC_FUEL_TYPES = frozenset({FuelType.PHEV, FuelType.EV})

# Chip/donut tint per fuel_type (all in .tint-* in app.css).
FUEL_TYPE_TINT = {
    FuelType.GASOLINE: "amber",
    FuelType.DIESEL: "slate",
    FuelType.HYBRID: "teal",
    FuelType.PHEV: "emerald",
    FuelType.EV: "sky",
    FuelType.OTHER: "violet",
}


class OwnershipMode(models.TextChoices):
    OWNED_CASH = "owned_cash", "Owned (paid in full)"
    OWNED_FINANCED = "owned_financed", "Owned (financed)"
    LEASED = "leased", "Leased"


OWNED_MODES = frozenset({OwnershipMode.OWNED_CASH, OwnershipMode.OWNED_FINANCED})


class MileageUnit(models.TextChoices):
    MILES = "mi", "Miles"
    KILOMETERS = "km", "Kilometres"


class DriverRole(models.TextChoices):
    PRIMARY_OWNER = "primary_owner", "Primary owner"
    CO_OWNER = "co_owner", "Co-owner"
    PRIMARY_DRIVER = "primary_driver", "Primary driver"
    ADDITIONAL_DRIVER = "additional_driver", "Additional driver"


# Order drivers primary-owner-first for display.
DRIVER_ROLE_ORDER = {
    DriverRole.PRIMARY_OWNER: 0,
    DriverRole.CO_OWNER: 1,
    DriverRole.PRIMARY_DRIVER: 2,
    DriverRole.ADDITIONAL_DRIVER: 3,
}
# Roles that make someone an owner of the vehicle (→ dealer "customer" P2O link).
OWNER_ROLES = frozenset({DriverRole.PRIMARY_OWNER, DriverRole.CO_OWNER})


class CostKind(models.TextChoices):
    PURCHASE = "purchase", "Purchase"
    IMPROVEMENT = "improvement", "Improvement / upgrade"
    FUEL = "fuel", "Fuel / charging"
    SERVICE = "service", "Service"
    REPAIR = "repair", "Repair"
    INSURANCE = "insurance", "Insurance"
    REGISTRATION = "registration", "Registration / road tax"
    INSPECTION = "inspection", "Inspection"
    EMISSIONS = "emissions", "Emissions / smog"
    LEASE_PAYMENT = "lease_payment", "Lease payment"
    LEASE_DEPOSIT = "lease_deposit", "Lease deposit"
    TAX_FEE = "tax_fee", "Tax / fee"
    PROPERTY_TAX = "property_tax", "Personal property tax"
    OTHER = "other", "Other"


# Kinds that CAPITALIZE into the vehicle's own asset node (1420.NN) rather than expensing.
CAPITALIZING_KINDS = frozenset({CostKind.PURCHASE, CostKind.IMPROVEMENT})
# Kinds that capitalize into the shared refundable-deposit asset (1320) instead of the vehicle node.
DEPOSIT_KINDS = frozenset({CostKind.LEASE_DEPOSIT})
# Kinds whose payment advances a renewal date (event.covers_through → the matching Vehicle field).
# Registration + inspection compliance moved to first-class dated records (VehicleRegistration /
# VehicleInspection) that own the next-due date; only auto insurance stays scalar/covers_through
# here (migrated into policies by the future Insurance module).
RENEWAL_KINDS = {
    CostKind.INSURANCE: "insurance_expiry",
}
# Kinds that advance a matching ServiceSchedule.
SERVICE_KINDS = frozenset({CostKind.SERVICE, CostKind.REPAIR})

COST_KIND_GLYPH = {
    CostKind.PURCHASE: "car",
    CostKind.IMPROVEMENT: "wrench",
    CostKind.FUEL: "fuel",
    CostKind.SERVICE: "wrench",
    CostKind.REPAIR: "wrench",
    CostKind.INSURANCE: "shield",
    CostKind.REGISTRATION: "file-text",
    CostKind.INSPECTION: "clipboard-check",
    CostKind.EMISSIONS: "leaf",  # gauge fallback if the icon set lacks "leaf"
    CostKind.LEASE_PAYMENT: "calendar-days",
    CostKind.LEASE_DEPOSIT: "piggy-bank",
    CostKind.TAX_FEE: "receipt",
    CostKind.PROPERTY_TAX: "landmark",
    CostKind.OTHER: "circle",
}


# --- Registration / compliance vocabularies (module 8 follow-up) -----------------------------

class PlateType(models.TextChoices):
    STANDARD = "standard", "Standard"
    VANITY = "vanity", "Vanity / personalised"
    SPECIALTY = "specialty", "Specialty"
    TEMPORARY = "temporary", "Temporary"
    DISABLED = "disabled", "Disabled"
    DEALER = "dealer", "Dealer"


class TitleStatus(models.TextChoices):
    CLEAN = "clean", "Clean"
    LIEN = "lien", "Lien (financed)"
    SALVAGE = "salvage", "Salvage"
    REBUILT = "rebuilt", "Rebuilt"


class RegistrationReason(models.TextChoices):
    INITIAL = "initial", "Initial registration"
    RENEWAL = "renewal", "Renewal"
    MOVED = "moved", "Moved jurisdiction"
    PLATE_CHANGE = "plate_change", "Plate change"
    TITLE_CHANGE = "title_change", "Title change"
    REPLACEMENT = "replacement", "Replacement"


class ComplianceKind(models.TextChoices):
    SAFETY = "safety", "Mechanical / safety inspection"       # annual
    EMISSIONS = "emissions", "Emissions test"                 # biennial
    COMBINED = "combined", "Safety + emissions"               # one sticker, both


class ComplianceResult(models.TextChoices):
    PASS = "pass", "Pass"
    FAIL = "fail", "Fail"
    CONDITIONAL = "conditional", "Conditional / advisory"
    NOT_REQUIRED = "not_required", "Not required / exempt"


# Pre-fill months for a record's next-due (expires_on stays user-editable).
COMPLIANCE_DEFAULT_MONTHS = {
    ComplianceKind.SAFETY: 12,
    ComplianceKind.EMISSIONS: 24,
    ComplianceKind.COMBINED: 12,
}
REGISTRATION_DEFAULT_MONTHS = 12

# A COMBINED test satisfies BOTH the mechanical and emissions next-due dates.
SATISFIES_SAFETY = frozenset({ComplianceKind.SAFETY, ComplianceKind.COMBINED})
SATISFIES_EMISSIONS = frozenset({ComplianceKind.EMISSIONS, ComplianceKind.COMBINED})

# Tint per compliance kind / result (donuts, badges — all .tint-*/variants in app.css).
COMPLIANCE_KIND_GLYPH = {
    ComplianceKind.SAFETY: "clipboard-check",
    ComplianceKind.EMISSIONS: "leaf",
    ComplianceKind.COMBINED: "shield-check",
}
COMPLIANCE_RESULT_TINT = {
    ComplianceResult.PASS: "emerald",
    ComplianceResult.FAIL: "rose",
    ComplianceResult.CONDITIONAL: "amber",
    ComplianceResult.NOT_REQUIRED: "slate",
}


class ServiceInvoiceCategory(models.TextChoices):
    SERVICE = "service", "Service"
    REPAIR = "repair", "Repair"


class FuelUnit(models.TextChoices):
    GALLON = "gal", "Gallons"
    LITRE = "l", "Litres"
    KWH = "kWh", "kWh"


class Funding(models.TextChoices):
    """How a cost event was (or wasn't) paid — hand-coded in the modals so payables' own Funding
    enum values can't leak in. NONE records an accrued (unpaid) bill only."""

    BANK = "bank", "Bank account"
    CARD = "card", "Credit card"
    CASH = "cash", "Cash / other"
    NONE = "none", "Unpaid (record bill only)"


class DisposalMethod(models.TextChoices):
    SALE = "sale", "Sold"
    TRADE_IN = "trade_in", "Traded in"
    TOTAL_LOSS = "total_loss", "Total loss / insurance write-off"
    GIFT = "gift", "Gifted / donated"
    SCRAP = "scrap", "Scrapped"
    LEASE_RETURN = "lease_return", "Lease returned"


def _money(**kw):
    return models.DecimalField(
        max_digits=AMOUNT_MAX_DIGITS, decimal_places=AMOUNT_DECIMALS, **kw
    )


class Vehicle(SoftDeleteModel):
    """A household vehicle. An owned vehicle carries its cost basis in the general ledger via
    `gl_account` (a postable node under `1420 Vehicles`); a leased vehicle has no GL node. This row
    holds the human-facing identity, ownership terms, parties, policy/renewal metadata and mileage.
    """

    # --- identity ---
    nickname = models.CharField(max_length=120)
    year = models.SmallIntegerField(null=True, blank=True)
    make = models.CharField(max_length=60, blank=True)
    # `model_name`: plain `model` clashes with Django internals.
    model_name = models.CharField(max_length=80, blank=True)
    trim = models.CharField(max_length=60, blank=True)
    body_type = models.CharField(max_length=40, blank=True)
    color = models.CharField(max_length=40, blank=True)
    fuel_type = models.CharField(max_length=10, choices=FuelType.choices, default=FuelType.GASOLINE)

    # --- ids / documents ---
    vin = models.CharField(max_length=40, blank=True)
    license_plate = models.CharField(max_length=20, blank=True)
    plate_jurisdiction = models.CharField(max_length=40, blank=True)
    title_number = models.CharField(max_length=60, blank=True)

    # --- ownership ---
    ownership_mode = models.CharField(
        max_length=16, choices=OwnershipMode.choices, default=OwnershipMode.OWNED_CASH
    )
    currency = models.ForeignKey("finance.Currency", on_delete=models.PROTECT, related_name="+")
    cost_basis = _money(default=ZERO)  # captured purchase price (leased: informational)
    acquired_year = models.SmallIntegerField(null=True, blank=True)
    acquired_month = models.SmallIntegerField(null=True, blank=True)
    acquired_day = models.SmallIntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    # --- links ---
    # The dedicated postable ledger node carrying this vehicle's cost (owned only); created by
    # apps.automobile.services.ensure_gl_account after the row is first saved.
    gl_account = models.OneToOneField(
        "finance.Account", on_delete=models.PROTECT, null=True, blank=True, related_name="vehicle"
    )
    loan = models.ForeignKey(
        "loans.Loan", on_delete=models.SET_NULL, null=True, blank=True, related_name="vehicles"
    )

    # --- lease terms (leased vehicles only) ---
    lease_monthly_payment = _money(null=True, blank=True)
    lease_start_date = models.DateField(null=True, blank=True)
    lease_end_date = models.DateField(null=True, blank=True)
    lease_term_months = models.PositiveSmallIntegerField(null=True, blank=True)
    lease_annual_mileage = models.PositiveIntegerField(null=True, blank=True)
    lease_residual = _money(null=True, blank=True)
    # The refundable security deposit paid at lease start — held as a 1320 asset (a shared leaf, not
    # a per-vehicle node), recovered on return / applied on buyout.
    lease_security_deposit = _money(default=ZERO)

    # --- parties ---
    dealer_organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    insurer_organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )

    # --- policy / renewal metadata ---
    insurance_carrier = models.CharField(max_length=120, blank=True)
    insurance_policy_number = models.CharField(max_length=80, blank=True)
    insurance_expiry = models.DateField(null=True, blank=True)
    # registration_expiry / inspection_due / emissions_due / property_tax_due are DENORM CACHES the
    # service rewrites from the latest VehicleRegistration / VehicleInspection / VehiclePropertyTax
    # record (records are the single source of truth). Kept so list search + the identity header +
    # the generic dashboard renewals loop keep working.
    registration_expiry = models.DateField(null=True, blank=True)
    inspection_due = models.DateField(null=True, blank=True)
    emissions_due = models.DateField(null=True, blank=True)
    property_tax_due = models.DateField(null=True, blank=True)
    # Persistent per-vehicle "compliance never required" (EV/new/classic) — suppresses reminders.
    # Composes with a one-off ComplianceResult.NOT_REQUIRED record (expires_on=None → no nag).
    inspection_exempt = models.BooleanField(default=False)
    emissions_exempt = models.BooleanField(default=False)
    warranty_provider = models.CharField(max_length=120, blank=True)
    warranty_expiry = models.DateField(null=True, blank=True)
    warranty_miles = models.PositiveIntegerField(null=True, blank=True)

    # --- mileage ---
    current_mileage = models.PositiveIntegerField(null=True, blank=True)
    mileage_unit = models.CharField(
        max_length=2, choices=MileageUnit.choices, default=MileageUnit.MILES
    )

    # --- disposal ---
    disposed_year = models.SmallIntegerField(null=True, blank=True)
    disposed_month = models.SmallIntegerField(null=True, blank=True)
    disposed_day = models.SmallIntegerField(null=True, blank=True)

    photo = models.ImageField(upload_to="vehicle_photos/", null=True, blank=True)
    notes = models.TextField(blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["nickname"]

    def __str__(self) -> str:
        return self.nickname

    # --- identity / type helpers ---
    @property
    def display(self) -> str:
        return self.nickname

    @property
    def full_name(self) -> str:
        """Year Make Model — the descriptive name (falls back to the nickname)."""
        parts = [str(self.year) if self.year else "", self.make, self.model_name, self.trim]
        label = " ".join(p for p in parts if p).strip()
        return label or self.nickname

    @property
    def fuel_label(self) -> str:
        return self.get_fuel_type_display()

    @property
    def type_tint(self) -> str:
        return FUEL_TYPE_TINT.get(self.fuel_type, "slate")

    @property
    def is_electric(self) -> bool:
        return self.fuel_type in ELECTRIC_FUEL_TYPES

    # --- ownership helpers ---
    @property
    def is_owned(self) -> bool:
        return self.ownership_mode in OWNED_MODES

    @property
    def is_financed(self) -> bool:
        return self.ownership_mode == OwnershipMode.OWNED_FINANCED

    @property
    def is_leased(self) -> bool:
        return self.ownership_mode == OwnershipMode.LEASED

    @property
    def ownership_label(self) -> str:
        return self.get_ownership_mode_display()

    # --- value (from the GL for owned; overlay for market value) ---
    @property
    def cost(self):
        """Book/cost basis, base currency. For an owned vehicle this is `account_balance(gl)` (it
        grows with capitalized improvements); a leased vehicle has no GL node → its `cost_basis`."""
        if self.is_owned and self.gl_account_id is not None:
            from apps.finance.services import account_balance

            return account_balance(self.gl_account)
        return self.cost_basis or ZERO

    def value_on(self, on_date=None):
        """The latest manual valuation on/before `on_date` (default today), else cost."""
        on_date = on_date or datetime.date.today()
        v = self.valuations.filter(as_of__lte=on_date).order_by("-as_of").first()
        return v.value if v is not None else self.cost

    @property
    def current_value(self):
        return self.value_on()

    @property
    def depreciation(self):
        """Cost − current market value (positive = value has fallen below cost)."""
        return self.cost - self.current_value

    # --- lifecycle dates ---
    @property
    def acquired(self) -> PartialDate:
        return PartialDate.from_instance(self, "acquired")

    @property
    def disposed(self) -> PartialDate:
        return PartialDate.from_instance(self, "disposed")

    @property
    def is_disposed(self) -> bool:
        return self.disposed.is_set or hasattr(self, "disposal")

    # --- renewal helpers ---
    @staticmethod
    def _days_until(when):
        return (when - datetime.date.today()).days if when else None

    @property
    def insurance_days_left(self):
        return self._days_until(self.insurance_expiry)

    @property
    def registration_days_left(self):
        return self._days_until(self.registration_expiry)

    @property
    def inspection_days_left(self):
        return self._days_until(self.inspection_due)

    @property
    def emissions_days_left(self):
        return self._days_until(self.emissions_due)

    @property
    def property_tax_days_left(self):
        return self._days_until(self.property_tax_due)

    @property
    def warranty_days_left(self):
        return self._days_until(self.warranty_expiry)

    # --- registration / compliance (records are the single source of truth) ---
    @property
    def current_registration(self):
        """The registration term in effect today — the latest with `effective_from ≤ today`
        (à la Loan.current_rate). None when the vehicle has no registration on/before today."""
        return (
            self.registrations.filter(effective_from__lte=datetime.date.today())
            .order_by("-effective_from", "-id")
            .first()
        )

    @property
    def current_plate(self) -> str:
        reg = self.current_registration
        return (reg.plate_number if reg else self.license_plate) or ""

    @property
    def current_plate_state(self) -> str:
        reg = self.current_registration
        return (reg.jurisdiction if reg else self.plate_jurisdiction) or ""

    @property
    def current_title_status(self):
        reg = self.current_registration
        return reg.title_status if reg else None

    @property
    def current_title_status_label(self) -> str:
        reg = self.current_registration
        return reg.title_status_label if reg else ""

    @property
    def lienholder(self):
        reg = self.current_registration
        return reg.lienholder if reg else None

    def latest_inspection(self, kind=None):
        """The most-recent inspection (optionally filtered to a single ComplianceKind)."""
        qs = self.inspections.all()
        if kind is not None:
            qs = qs.filter(kind=kind)
        return qs.order_by("-performed_on", "-id").first()

    @property
    def latest_property_tax(self):
        return self.property_taxes.order_by("-tax_year", "-id").first()

    @property
    def lease_days_left(self):
        return self._days_until(self.lease_end_date)

    # --- mileage / lease meter ---
    @property
    def initial_mileage(self):
        """The odometer reading captured at acquisition (drives MPG baseline + the lease meter)."""
        first = self.odometer_readings.order_by("as_of", "id").first()
        return first.mileage if first else None

    @property
    def lease_mileage_allowance(self):
        """Total contracted mileage over the lease term (annual allowance × years)."""
        if not (self.is_leased and self.lease_annual_mileage and self.lease_term_months):
            return None
        from decimal import Decimal

        return int(Decimal(self.lease_annual_mileage) * Decimal(self.lease_term_months) / 12)

    @property
    def lease_mileage_used(self):
        """Miles driven since lease start (current − initial reading), for the c-meter vs the
        allowance. None when we can't compute it."""
        if not self.is_leased or self.current_mileage is None:
            return None
        base = self.initial_mileage or 0
        used = self.current_mileage - base
        return used if used > 0 else 0


class VehicleDriver(TimeStampedModel):
    """A household member on the vehicle, with a role (mirrors LoanBorrower). The primary owner is
    linked to the dealer ('customer' P2O); every driver is linked to the insurer ('insured')."""

    vehicle = models.ForeignKey(Vehicle, on_delete=models.CASCADE, related_name="drivers")
    person = models.ForeignKey(
        "contacts.Person", on_delete=models.PROTECT, related_name="vehicle_drivings"
    )
    role = models.CharField(
        max_length=18, choices=DriverRole.choices, default=DriverRole.PRIMARY_OWNER
    )

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["vehicle", "person"], name="vehicledriver_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.person} ({self.get_role_display()})"

    @property
    def role_label(self) -> str:
        return self.get_role_display()

    @property
    def role_order(self) -> int:
        return DRIVER_ROLE_ORDER.get(self.role, 9)

    @property
    def is_owner(self) -> bool:
        return self.role in OWNER_ROLES


class VehicleCostEvent(SoftDeleteModel):
    """A money event for a vehicle (purchase / improvement / fuel / service / insurance / ...). Its
    GL effect lives entirely on a linked locked `payables.Bill` (and, when funded, a locked
    `payables.Payment`) — this row carries none of its own journal entry. Exactly one vendor party
    is required (a vendor-less event cannot create a bill)."""

    vehicle = models.ForeignKey(Vehicle, on_delete=models.CASCADE, related_name="cost_events")
    kind = models.CharField(max_length=16, choices=CostKind.choices, default=CostKind.FUEL)
    date = models.DateField()
    amount = _money()  # > 0

    # Vendor: a Person OR an Organization (exactly one — the bill's vendor).
    vendor_person = models.ForeignKey(
        "contacts.Person", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="vehicle_cost_events",
    )
    vendor_organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="vehicle_cost_events",
    )

    # The locked payables document(s) this event owns (direct links; the payables source GFK is
    # unindexed). `payment` is the module-created funding payment, kept for teardown.
    bill = models.OneToOneField(
        "payables.Bill", on_delete=models.SET_NULL, null=True, blank=True, related_name="cost_event"
    )
    payment = models.ForeignKey(
        "payables.Payment", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    # Fuel / charging.
    fuel_volume = _money(null=True, blank=True)
    fuel_unit = models.CharField(max_length=4, choices=FuelUnit.choices, blank=True)
    odometer = models.PositiveIntegerField(null=True, blank=True)
    is_full_tank = models.BooleanField(default=True)

    # Advances the matching renewal date on the vehicle (insurance / registration / inspection).
    covers_through = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)  # → bill.due_date (payables aging)

    # Funding hint (drives the optional locked payment). NONE records an unpaid bill only.
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

    memo = models.CharField(max_length=255, blank=True)
    reference = models.CharField(max_length=80, blank=True)  # → bill.vendor_ref

    history = HistoricalRecords()

    class Meta:
        ordering = ["-date", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(vendor_person__isnull=False, vendor_organization__isnull=True)
                    | models.Q(vendor_person__isnull=True, vendor_organization__isnull=False)
                ),
                name="vehiclecostevent_one_vendor",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="vehiclecostevent_amount_positive"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.amount} on {self.date}"

    # Discriminator for the merged cost register (a service invoice is the other member — see
    # services.register). A plain cost event is never a service invoice.
    is_service_invoice = False

    @property
    def kind_label(self) -> str:
        return self.get_kind_display()

    @property
    def kind_glyph(self) -> str:
        return COST_KIND_GLYPH.get(self.kind, "circle")

    @property
    def is_capitalizing(self) -> bool:
        return self.kind in CAPITALIZING_KINDS

    @property
    def is_deposit(self) -> bool:
        return self.kind in DEPOSIT_KINDS

    @property
    def vendor(self):
        return self.vendor_person or self.vendor_organization

    @property
    def vendor_kind(self) -> str:
        return "person" if self.vendor_person_id else "organization"

    @property
    def vendor_name(self) -> str:
        party = self.vendor
        if party is None:
            return ""
        for attr in ("display_name", "full_name", "name"):
            val = getattr(party, attr, "")
            if val:
                return val
        return str(party)

    @property
    def is_funded(self) -> bool:
        return self.funding_source in (Funding.BANK, Funding.CARD, Funding.CASH)

    # Duck-typed hooks read by the Payables locked-bill/payment back-link (kept module-agnostic
    # there): the owning module's human label + the tenant-relative path back to the record.
    @property
    def managed_label(self) -> str:
        return f"Vehicle · {self.vehicle.nickname}"

    @property
    def managed_url(self) -> str:
        return f"automobile/{self.vehicle_id}/"


class VehicleValuation(TimeStampedModel):
    """A dated manual mark of a vehicle's market value (twin of `investments.SecurityPrice`). The
    latest on/before a date is the value shown; a display-only overlay that posts nothing to the GL.
    """

    vehicle = models.ForeignKey(Vehicle, on_delete=models.CASCADE, related_name="valuations")
    as_of = models.DateField()
    value = _money()
    source = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["-as_of"]
        constraints = [
            models.UniqueConstraint(fields=["vehicle", "as_of"], name="vehiclevaluation_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.vehicle_id} @ {self.as_of}: {self.value}"


class OdometerReading(TimeStampedModel):
    """A dated mileage reading (one per vehicle per day). The acquisition flow records the initial
    reading (MPG baseline + lease-mileage meter); fuel/service events upsert one from their
    odometer. Distinct from the fuel-economy log, which reads fuel cost events directly."""

    class Source(models.TextChoices):
        MANUAL = "manual", "Manual"
        FUEL = "fuel", "Fuel"
        SERVICE = "service", "Service"
        PURCHASE = "purchase", "Purchase"

    vehicle = models.ForeignKey(
        Vehicle, on_delete=models.CASCADE, related_name="odometer_readings"
    )
    as_of = models.DateField()
    mileage = models.PositiveIntegerField()
    source = models.CharField(max_length=8, choices=Source.choices, default=Source.MANUAL)
    note = models.CharField(max_length=160, blank=True)

    class Meta:
        ordering = ["-as_of", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["vehicle", "as_of"], name="odometerreading_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.mileage} on {self.as_of}"


class ServiceSchedule(SoftDeleteModel):
    """A recurring maintenance item for a vehicle, due by elapsed months and/or driven miles. A
    matching service/repair cost event advances `last_done_*` and rolls the next-due forward."""

    vehicle = models.ForeignKey(
        Vehicle, on_delete=models.CASCADE, related_name="service_schedules"
    )
    name = models.CharField(max_length=120)
    interval_months = models.PositiveSmallIntegerField(null=True, blank=True)
    interval_miles = models.PositiveIntegerField(null=True, blank=True)
    last_done_date = models.DateField(null=True, blank=True)
    last_done_mileage = models.PositiveIntegerField(null=True, blank=True)
    next_due_date = models.DateField(null=True, blank=True)
    next_due_mileage = models.PositiveIntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["name", "id"]

    def __str__(self) -> str:
        return self.name

    @property
    def days_to_due(self):
        if not self.next_due_date:
            return None
        return (self.next_due_date - datetime.date.today()).days

    @property
    def miles_to_due(self):
        if self.next_due_mileage is None or self.vehicle.current_mileage is None:
            return None
        return self.next_due_mileage - self.vehicle.current_mileage

    @property
    def is_overdue(self) -> bool:
        days = self.days_to_due
        miles = self.miles_to_due
        return bool((days is not None and days < 0) or (miles is not None and miles < 0))

    @property
    def due_soon(self) -> bool:
        if self.is_overdue:
            return False
        days = self.days_to_due
        miles = self.miles_to_due
        return bool((days is not None and days <= 30) or (miles is not None and miles <= 1000))


class VehicleDisposal(SoftDeleteModel):
    """The full disposal of a vehicle — a direct finance entry (not a bill) booking proceeds vs cost
    with the difference to a single gain/loss account (`4930`). Proceeds to a tracked bank account
    route via `1150` + a native banking TRANSFER_IN leg (`bank_txn`); a trade-in allowance clears
    via a `1150` Payment (`trade_payment`) allocated to the replacement vehicle's dealer bill."""

    vehicle = models.OneToOneField(Vehicle, on_delete=models.CASCADE, related_name="disposal")
    method = models.CharField(
        max_length=12, choices=DisposalMethod.choices, default=DisposalMethod.SALE
    )
    date = models.DateField()
    proceeds = _money(default=ZERO)
    odometer = models.PositiveIntegerField(null=True, blank=True)

    # Buyer (at most one) — the party dimension on the proceeds leg.
    buyer_person = models.ForeignKey(
        "contacts.Person", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    buyer_organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )

    # Proceeds routing: a tracked bank account (→ native TRANSFER_IN leg), else cash / external.
    proceeds_account = models.ForeignKey(
        "banking.BankAccount", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    bank_txn = models.ForeignKey(
        "banking.BankTransaction", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    # On a trade-in, the 1150-clearing Payment allocated to the replacement's dealer bill.
    trade_payment = models.ForeignKey(
        "payables.Payment", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    journal_entry = models.ForeignKey(
        "finance.JournalEntry", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    posting_version = models.PositiveIntegerField(default=1)
    notes = models.TextField(blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["-date", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(
                    buyer_person__isnull=False, buyer_organization__isnull=False
                ),
                name="vehicledisposal_one_buyer",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_method_display()} — {self.vehicle_id}"

    @property
    def method_label(self) -> str:
        return self.get_method_display()

    @property
    def is_lease_return(self) -> bool:
        return self.method == DisposalMethod.LEASE_RETURN

    @property
    def gain_loss(self):
        """Proceeds − book cost (positive = gain). Once posted, read from the booked 4930 line so
        it stays correct after the vehicle node is derecognized to zero; before posting it's a live
        estimate. Not meaningful for a lease return (no owned node → no 4930 line, returns 0)."""
        if self.journal_entry_id is not None:
            line = self.journal_entry.lines.filter(
                account__system_key="asset_disposal_gain_loss"
            ).first()
            return (line.base_credit - line.base_debit) if line is not None else ZERO
        return self.proceeds - self.vehicle.cost

    @property
    def buyer(self):
        return self.buyer_person or self.buyer_organization


# --- Registration / inspection / property-tax records ----------------------------------------
# Dated event logs (TimeStampedModel, no HistoricalRecords/soft-delete): the row-set IS the
# history ("current = latest ≤ today"), mirroring VehicleValuation / LoanRateChange. Each posts
# NOTHING to the GL itself — its optional `fee_event` (a SoftDeleteModel) owns the money side, so
# the audit trail lives on the locked bill.


class VehicleRegistration(TimeStampedModel):
    """One registration term for a vehicle — it *is* the plate / title / jurisdiction history
    because each row snapshots them. Registration expiry drives the next-due reminder."""

    vehicle = models.ForeignKey(Vehicle, on_delete=models.CASCADE, related_name="registrations")
    jurisdiction = models.CharField(max_length=40)  # state/province, free text
    plate_number = models.CharField(max_length=20, blank=True)
    plate_type = models.CharField(
        max_length=12, choices=PlateType.choices, default=PlateType.STANDARD
    )
    title_number = models.CharField(max_length=60, blank=True)
    title_jurisdiction = models.CharField(max_length=40, blank=True)
    title_status = models.CharField(
        max_length=10, choices=TitleStatus.choices, default=TitleStatus.CLEAN
    )
    # The lender holding the title while the vehicle is financed (a plain vendor org).
    lienholder_organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    effective_from = models.DateField()
    expires_on = models.DateField(null=True, blank=True)  # authoritative registration next-due
    reason = models.CharField(
        max_length=14, choices=RegistrationReason.choices, default=RegistrationReason.RENEWAL
    )
    fee_event = models.OneToOneField(
        VehicleCostEvent, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="registration",
    )
    document = models.FileField(upload_to="vehicle_docs/", null=True, blank=True)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-effective_from", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["vehicle", "effective_from"], name="vehicleregistration_unique"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.jurisdiction} {self.plate_number} from {self.effective_from}"

    @property
    def is_expired(self) -> bool:
        return bool(self.expires_on and self.expires_on < datetime.date.today())

    @property
    def days_left(self):
        return (self.expires_on - datetime.date.today()).days if self.expires_on else None

    @property
    def is_current(self) -> bool:
        current = self.vehicle.current_registration
        return current is not None and current.pk == self.pk

    @property
    def reason_label(self) -> str:
        return self.get_reason_display()

    @property
    def plate_type_label(self) -> str:
        return self.get_plate_type_display()

    @property
    def title_status_label(self) -> str:
        return self.get_title_status_display()

    @property
    def lienholder(self):
        return self.lienholder_organization


class VehicleInspection(TimeStampedModel):
    """A safety / emissions / combined inspection event. A COMBINED test satisfies both the
    mechanical and emissions next-due dates. `expires_on` is the authoritative next-due."""

    vehicle = models.ForeignKey(Vehicle, on_delete=models.CASCADE, related_name="inspections")
    kind = models.CharField(
        max_length=10, choices=ComplianceKind.choices, default=ComplianceKind.SAFETY
    )
    performed_on = models.DateField()
    result = models.CharField(
        max_length=12, choices=ComplianceResult.choices, default=ComplianceResult.PASS
    )
    expires_on = models.DateField(null=True, blank=True)  # authoritative next-due
    station_organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    certificate_number = models.CharField(max_length=60, blank=True)
    odometer = models.PositiveIntegerField(null=True, blank=True)
    fee_event = models.OneToOneField(
        VehicleCostEvent, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="inspection",
    )
    document = models.FileField(upload_to="vehicle_docs/", null=True, blank=True)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-performed_on", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["vehicle", "kind", "performed_on"], name="vehicleinspection_unique"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} on {self.performed_on}"

    @property
    def is_expired(self) -> bool:
        return bool(self.expires_on and self.expires_on < datetime.date.today())

    @property
    def days_left(self):
        return (self.expires_on - datetime.date.today()).days if self.expires_on else None

    @property
    def passed(self) -> bool:
        return self.result in (ComplianceResult.PASS, ComplianceResult.CONDITIONAL)

    @property
    def is_exempt(self) -> bool:
        return self.result == ComplianceResult.NOT_REQUIRED

    @property
    def kind_label(self) -> str:
        return self.get_kind_display()

    @property
    def result_label(self) -> str:
        return self.get_result_display()

    @property
    def result_tint(self) -> str:
        return COMPLIANCE_RESULT_TINT.get(self.result, "slate")

    @property
    def glyph(self) -> str:
        return COMPLIANCE_KIND_GLYPH.get(self.kind, "clipboard-check")


class VehiclePropertyTax(TimeStampedModel):
    """A personal-property (ad-valorem) tax assessment for one tax year — the annual county/city
    tax several US states levy on a vehicle by assessed value. The `amount` IS the bill: it always
    routes through Payables (funded or accrued), unlike optional compliance fees."""

    vehicle = models.ForeignKey(Vehicle, on_delete=models.CASCADE, related_name="property_taxes")
    tax_year = models.PositiveSmallIntegerField()
    jurisdiction = models.CharField(max_length=60)  # taxing county / city
    assessed_value = _money(null=True, blank=True)
    rate = models.DecimalField(max_digits=7, decimal_places=4, null=True, blank=True)
    amount = _money()  # the tax due → the bill total (> 0)
    assessed_on = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)  # → bill.due_date + reminder
    fee_event = models.OneToOneField(
        VehicleCostEvent, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="property_tax",
    )
    document = models.FileField(upload_to="vehicle_docs/", null=True, blank=True)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-tax_year", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["vehicle", "tax_year"], name="vehiclepropertytax_unique"
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="vehiclepropertytax_amount_positive"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.tax_year} property tax — {self.vehicle_id}"

    @property
    def is_paid(self) -> bool:
        """True once the fee bill is fully paid (clears the reminder)."""
        ev = self.fee_event
        return bool(ev and ev.bill_id and ev.bill.status == "paid")

    @property
    def days_to_due(self):
        return (self.due_date - datetime.date.today()).days if self.due_date else None

    @property
    def is_overdue(self) -> bool:
        return bool(self.due_date and not self.is_paid and self.due_date < datetime.date.today())


# --- Rich multi-line service invoices (header → jobs → parts) --------------------------------
# A first-class financial document that owns a locked, multi-line bill (only category totals flow
# to the AP bill; the op-code/parts granularity stays in the Auto module). Coexists with the
# single-line Service/Repair VehicleCostEvent.


class VehicleServiceInvoice(SoftDeleteModel):
    """A shop / dealer service invoice (repair-order): a header + several jobs, each with parts,
    plus a totals box. Materializes a locked multi-line `payables.Bill` (and an optional locked
    Payment) whose category lines sum to the grand total. A $0 (all-warranty) invoice records
    history + advances schedules but creates NO bill."""

    vehicle = models.ForeignKey(
        Vehicle, on_delete=models.CASCADE, related_name="service_invoices"
    )
    date = models.DateField()

    # Vendor: a Person OR an Organization (exactly one — the bill's vendor / the shop).
    vendor_person = models.ForeignKey(
        "contacts.Person", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    vendor_organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )

    invoice_number = models.CharField(max_length=40, blank=True)  # RO / invoice # → bill.vendor_ref
    service_advisor = models.CharField(max_length=80, blank=True)
    odometer_in = models.PositiveIntegerField(null=True, blank=True)
    odometer_out = models.PositiveIntegerField(null=True, blank=True)
    category = models.CharField(
        max_length=8, choices=ServiceInvoiceCategory.choices,
        default=ServiceInvoiceCategory.SERVICE,
    )

    # Totals-breakdown header amounts (jobs carry labor; parts carry parts).
    sublet = _money(default=ZERO)
    shop_supplies = _money(default=ZERO)
    discount = _money(default=ZERO)
    sales_tax = _money(default=ZERO)

    bill = models.OneToOneField(
        "payables.Bill", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="service_invoice",
    )
    payment = models.ForeignKey(
        "payables.Payment", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    # Funding hint (drives the optional locked payment) — same shape as VehicleCostEvent.
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

    document = models.FileField(upload_to="vehicle_docs/", null=True, blank=True)
    reference = models.CharField(max_length=80, blank=True)
    memo = models.CharField(max_length=255, blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["-date", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(vendor_person__isnull=False, vendor_organization__isnull=True)
                    | models.Q(vendor_person__isnull=True, vendor_organization__isnull=False)
                ),
                name="serviceinvoice_one_vendor",
            ),
        ]

    def __str__(self) -> str:
        return f"Service invoice {self.invoice_number or self.pk} — {self.vehicle_id}"

    # Discriminator for the merged cost register (see services.register).
    is_service_invoice = True

    @property
    def kind_label(self) -> str:
        return "Service invoice"

    @property
    def kind_glyph(self) -> str:
        return "wrench"

    @property
    def category_label(self) -> str:
        return self.get_category_display()

    @property
    def vendor(self):
        return self.vendor_person or self.vendor_organization

    @property
    def vendor_kind(self) -> str:
        return "person" if self.vendor_person_id else "organization"

    @property
    def vendor_name(self) -> str:
        party = self.vendor
        if party is None:
            return ""
        for attr in ("display_name", "full_name", "name"):
            val = getattr(party, attr, "")
            if val:
                return val
        return str(party)

    @property
    def is_funded(self) -> bool:
        return self.funding_source in (Funding.BANK, Funding.CARD, Funding.CASH)

    # Derived totals.
    @property
    def parts_total(self):
        return sum(
            (p.amount for job in self.jobs.all() for p in job.parts.all()), ZERO
        )

    @property
    def labor_total(self):
        return sum((job.labor_amount for job in self.jobs.all()), ZERO)

    @property
    def grand_total(self):
        return (
            self.labor_total + self.parts_total + (self.sublet or ZERO)
            + (self.shop_supplies or ZERO) + (self.sales_tax or ZERO) - (self.discount or ZERO)
        )

    @property
    def amount(self):
        """Alias used by the merged cost register (shared shape with VehicleCostEvent.amount)."""
        return self.grand_total

    # Payables locked-bill/payment back-link hooks (module-agnostic there), like VehicleCostEvent.
    @property
    def managed_label(self) -> str:
        return f"Vehicle · {self.vehicle.nickname}"

    @property
    def managed_url(self) -> str:
        return f"automobile/{self.vehicle_id}/"


class VehicleServiceJob(TimeStampedModel):
    """A job / complaint line on a service invoice (invoice line A/B/C), rewritten on each save
    (like bill lines). Carries the op-code, the customer complaint, the technician and the labor
    charge; its parts hang beneath it."""

    invoice = models.ForeignKey(
        VehicleServiceInvoice, on_delete=models.CASCADE, related_name="jobs"
    )
    order = models.PositiveIntegerField(default=0)
    code = models.CharField(max_length=40, blank=True)  # op-code (MPI / PFL / BSFLUSH)
    complaint = models.CharField(max_length=200, blank=True)  # "CUSTOMER STATES …"
    description = models.CharField(max_length=255, blank=True)  # op-code description
    technician = models.CharField(max_length=40, blank=True)
    labor_hours = models.DecimalField(
        max_digits=AMOUNT_MAX_DIGITS, decimal_places=AMOUNT_DECIMALS, null=True, blank=True
    )
    labor_amount = _money(default=ZERO)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self) -> str:
        return self.code or self.complaint or f"Job {self.pk}"

    @property
    def parts_amount(self):
        return sum((p.amount for p in self.parts.all()), ZERO)


class VehicleServicePart(TimeStampedModel):
    """A part consumed by a service job (part # / qty / unit price)."""

    job = models.ForeignKey(VehicleServiceJob, on_delete=models.CASCADE, related_name="parts")
    order = models.PositiveIntegerField(default=0)
    part_number = models.CharField(max_length=60, blank=True)
    description = models.CharField(max_length=160, blank=True)
    quantity = models.DecimalField(
        max_digits=AMOUNT_MAX_DIGITS, decimal_places=AMOUNT_DECIMALS, default=Decimal("1")
    )
    unit_price = _money(default=ZERO)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self) -> str:
        return self.part_number or self.description or f"Part {self.pk}"

    @property
    def amount(self):
        return (self.quantity or ZERO) * (self.unit_price or ZERO)
