"""Real Estate (Plan C) — a household property register that puts OWNED property on the balance
sheet at cost and routes every money event through the existing Payables module as a READ-ONLY
(locked) bill. A close clone of the Automobile module (module 8), owned-only (no leasing).

An owned property owns one postable `finance.Account` nested under the `1410 Real Estate` header
(held at cost); every running cost — property tax, maintenance, HOA, utilities, improvements — is a
`PropertyCostEvent` that materializes a locked `payables.Bill` (and, when funded, a locked
`payables.Payment`); the acquisition is a locked, capitalizing bill (a mortgaged purchase settles it
with a down payment + a `Payment.Funding.LOAN` disbursement against the linked mortgage). A full
**disposal** (sale / gift / transfer) is a direct finance entry booking gain/loss to `4930`.

The GL effect of a cost event lives entirely on its linked Bill/Payment (no `journal_entry` on the
event); only a disposal posts a journal entry directly. Market value over time is a manual dated
overlay (`PropertyValuation`, like `automobile.VehicleValuation`) that posts nothing — net worth
stays at cost. Soft-deletable + audited like every tenant model (§5).
"""

import datetime

from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.models import SoftDeleteModel, TimeStampedModel
from apps.core.partialdate import PartialDate
from apps.finance.models import AMOUNT_DECIMALS, AMOUNT_MAX_DIGITS, ZERO

# The child models (PropertyOwner / PropertyCostEvent / PropertyDisposal) name their FK to Property
# `property` — the natural accessor used across the services / views / templates. That shadows the
# built-in `property` decorator inside those class bodies, so bind an alias for those attrs.
_prop = property


class OwnershipMode(models.TextChoices):
    OWNED_CASH = "owned_cash", "Owned (paid in full)"
    OWNED_FINANCED = "owned_financed", "Owned (mortgaged)"


OWNED_MODES = frozenset({OwnershipMode.OWNED_CASH, OwnershipMode.OWNED_FINANCED})


class PropertyType(models.TextChoices):
    SINGLE_FAMILY = "single_family", "Single-family home"
    CONDO = "condo", "Condominium"
    TOWNHOUSE = "townhouse", "Townhouse"
    MULTI_FAMILY = "multi_family", "Multi-family"
    LAND = "land", "Land / lot"
    COMMERCIAL = "commercial", "Commercial"
    OTHER = "other", "Other"


class PropertyUse(models.TextChoices):
    PRIMARY_RESIDENCE = "primary_residence", "Primary residence"
    SECOND_HOME = "second_home", "Second home"
    RENTAL_INVESTMENT = "rental_investment", "Rental / investment"
    LAND = "land", "Land"
    OTHER = "other", "Other"


class OwnerRole(models.TextChoices):
    OWNER = "owner", "Owner"
    CO_OWNER = "co_owner", "Co-owner"
    OCCUPANT = "occupant", "Occupant"


# Roles that put someone on title (display ordering only; no P2O).
OWNER_ROLES = frozenset({OwnerRole.OWNER, OwnerRole.CO_OWNER})
OWNER_ROLE_ORDER = {OwnerRole.OWNER: 0, OwnerRole.CO_OWNER: 1, OwnerRole.OCCUPANT: 2}


class CostKind(models.TextChoices):
    PURCHASE = "purchase", "Purchase"
    IMPROVEMENT = "improvement", "Improvement / renovation"
    CLOSING_COST = "closing_cost", "Closing costs"
    PROPERTY_TAX = "property_tax", "Property tax"
    MAINTENANCE = "maintenance", "Maintenance / repair"
    HOA = "hoa", "HOA / condo fees"
    UTILITIES = "utilities", "Utilities"
    OTHER = "other", "Other"


# Kinds that CAPITALIZE into the property's own asset node (1410.NN) rather than expensing. Closing
# costs capitalize into basis (an accepted simplification — strictly only some closing costs are).
CAPITALIZING_KINDS = frozenset(
    {CostKind.PURCHASE, CostKind.IMPROVEMENT, CostKind.CLOSING_COST}
)

# CostKind → (Expert activity key, Standard default account). Property tax posts to the GENERIC
# 5810 (property_tax_expense) — NOT the 5140 mortgage-escrow tax the Loans module already books from
# a mortgage payment (routing here too would double-count). Directly-paid tax only.
KIND_ACTIVITY = {
    CostKind.PROPERTY_TAX: ("property_tax", "property_tax_expense"),
    CostKind.MAINTENANCE: ("maintenance", "5130"),
    CostKind.HOA: ("hoa", "hoa_fees"),
    CostKind.UTILITIES: ("utilities", "5120"),
    CostKind.OTHER: (None, "5900"),
}

# Running-cost kinds counted in a property's total cost of ownership (not capitalizing).
RUNNING_COST_KINDS = frozenset(
    {CostKind.PROPERTY_TAX, CostKind.MAINTENANCE, CostKind.HOA, CostKind.UTILITIES, CostKind.OTHER}
)


class Funding(models.TextChoices):
    """How a cost event was (or wasn't) paid — hand-coded in the modals so payables' own Funding
    enum values can't leak in. NONE records an accrued (unpaid) bill only."""

    BANK = "bank", "Bank account"
    CARD = "card", "Credit card"
    CASH = "cash", "Cash / other"
    NONE = "none", "Unpaid (record bill only)"


class DisposalMethod(models.TextChoices):
    SALE = "sale", "Sold"
    GIFT = "gift", "Gifted / donated"
    TRANSFER = "transfer", "Transferred"


# Chip/donut tint per property type (all in .tint-* in app.css).
PROPERTY_TYPE_TINT = {
    PropertyType.SINGLE_FAMILY: "teal",
    PropertyType.CONDO: "sky",
    PropertyType.TOWNHOUSE: "violet",
    PropertyType.MULTI_FAMILY: "blue",
    PropertyType.LAND: "emerald",
    PropertyType.COMMERCIAL: "amber",
    PropertyType.OTHER: "slate",
}

# Glyphs chosen from the current icon sprite (templates/_icon_sprite.html) so nothing renders blank.
PROPERTY_TYPE_GLYPH = {
    PropertyType.SINGLE_FAMILY: "house",
    PropertyType.CONDO: "building-2",
    PropertyType.TOWNHOUSE: "house",
    PropertyType.MULTI_FAMILY: "building-2",
    PropertyType.LAND: "map",
    PropertyType.COMMERCIAL: "building-2",
    PropertyType.OTHER: "house",
}

COST_KIND_GLYPH = {
    CostKind.PURCHASE: "house",
    CostKind.IMPROVEMENT: "sparkles",
    CostKind.CLOSING_COST: "file-text",
    CostKind.PROPERTY_TAX: "landmark",
    CostKind.MAINTENANCE: "settings",
    CostKind.HOA: "building-2",
    CostKind.UTILITIES: "droplet",
    CostKind.OTHER: "coins",
}


def _money(**kw):
    return models.DecimalField(
        max_digits=AMOUNT_MAX_DIGITS, decimal_places=AMOUNT_DECIMALS, **kw
    )


class Property(SoftDeleteModel):
    """A household real-estate property. Owned-only in Phase 1: carries its cost basis in the GL via
    `gl_account` (a postable node under `1410 Real Estate`). Holds the identity, address, ownership
    terms, the optional mortgage link and lifecycle dates."""

    # --- identity ---
    nickname = models.CharField(max_length=120)
    property_type = models.CharField(
        max_length=16, choices=PropertyType.choices, default=PropertyType.SINGLE_FAMILY
    )
    use = models.CharField(
        max_length=18, choices=PropertyUse.choices, default=PropertyUse.PRIMARY_RESIDENCE
    )

    # --- address (inline) ---
    address_line1 = models.CharField(max_length=160, blank=True)
    address_line2 = models.CharField(max_length=160, blank=True)
    city = models.CharField(max_length=80, blank=True)
    state = models.CharField(max_length=80, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    country = models.CharField(max_length=60, blank=True)

    # --- ownership ---
    ownership_mode = models.CharField(
        max_length=16, choices=OwnershipMode.choices, default=OwnershipMode.OWNED_CASH
    )
    currency = models.ForeignKey("finance.Currency", on_delete=models.PROTECT, related_name="+")
    cost_basis = _money(default=ZERO)  # captured purchase price (informational; GL holds the truth)
    acquired_year = models.SmallIntegerField(null=True, blank=True)
    acquired_month = models.SmallIntegerField(null=True, blank=True)
    acquired_day = models.SmallIntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    # --- links ---
    # The dedicated postable ledger node carrying this property's cost; created by
    # apps.realestate.services.ensure_gl_account after the row is first saved.
    gl_account = models.OneToOneField(
        "finance.Account", on_delete=models.PROTECT, null=True, blank=True,
        related_name="realestate_property",
    )
    # The mortgage financing this property (managed in Loans). Set on a financed purchase.
    mortgage_loan = models.ForeignKey(
        "loans.Loan", on_delete=models.SET_NULL, null=True, blank=True, related_name="properties"
    )

    # --- parties ---
    seller_organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )

    # --- disposal ---
    disposed_year = models.SmallIntegerField(null=True, blank=True)
    disposed_month = models.SmallIntegerField(null=True, blank=True)
    disposed_day = models.SmallIntegerField(null=True, blank=True)

    notes = models.TextField(blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["nickname"]
        verbose_name_plural = "properties"

    def __str__(self) -> str:
        return self.nickname

    # --- identity / type helpers ---
    @property
    def display(self) -> str:
        return self.nickname

    @property
    def full_name(self) -> str:
        return self.nickname

    @property
    def type_label(self) -> str:
        return self.get_property_type_display()

    @property
    def use_label(self) -> str:
        return self.get_use_display()

    @property
    def type_tint(self) -> str:
        return PROPERTY_TYPE_TINT.get(self.property_type, "slate")

    @property
    def type_glyph(self) -> str:
        return PROPERTY_TYPE_GLYPH.get(self.property_type, "house")

    @property
    def address_oneline(self) -> str:
        parts = [self.address_line1, self.address_line2, self.city, self.state, self.postal_code]
        return ", ".join(p for p in parts if p).strip(", ")

    # --- ownership helpers ---
    @property
    def is_financed(self) -> bool:
        return self.ownership_mode == OwnershipMode.OWNED_FINANCED

    @property
    def ownership_label(self) -> str:
        return self.get_ownership_mode_display()

    # --- value (from the GL for cost; overlay for market value) ---
    @property
    def cost(self):
        """Book/cost basis in base currency = `account_balance(gl)` (grows with capitalized
        improvements/closing costs); falls back to `cost_basis` before the node exists."""
        if self.gl_account_id is not None:
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
    def appreciation(self):
        """Current market value − cost (positive = value has risen above cost)."""
        return self.current_value - self.cost

    @property
    def equity(self):
        """Market value − outstanding mortgage balance (the household's stake). Falls back to cost
        when there is no valuation."""
        mortgage = ZERO
        if self.mortgage_loan_id is not None:
            bal = getattr(self.mortgage_loan, "balance", None)
            mortgage = bal if bal is not None else ZERO
        return self.current_value - mortgage

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


class PropertyOwner(TimeStampedModel):
    """A household member on the property, with a role (mirrors VehicleDriver / LoanBorrower). Title
    holders + occupants; no P2O link (owners are people, not org-linked)."""

    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name="owners")
    person = models.ForeignKey(
        "contacts.Person", on_delete=models.PROTECT, related_name="property_ownerships"
    )
    role = models.CharField(max_length=10, choices=OwnerRole.choices, default=OwnerRole.OWNER)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["property", "person"], name="propertyowner_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.person} ({self.get_role_display()})"

    @_prop
    def role_label(self) -> str:
        return self.get_role_display()

    @_prop
    def role_order(self) -> int:
        return OWNER_ROLE_ORDER.get(self.role, 9)


class PropertyCostEvent(SoftDeleteModel):
    """A money event for a property (purchase / improvement / property tax / maintenance / HOA / …).
    Its GL effect lives entirely on a linked locked `payables.Bill` (and, when funded, a locked
    `payables.Payment`) — no journal entry of its own. Exactly one vendor party is required."""

    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name="cost_events")
    kind = models.CharField(max_length=16, choices=CostKind.choices, default=CostKind.MAINTENANCE)
    date = models.DateField()
    amount = _money()  # > 0

    # Vendor: a Person OR an Organization (exactly one — the bill's vendor).
    vendor_person = models.ForeignKey(
        "contacts.Person", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="property_cost_events",
    )
    vendor_organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="property_cost_events",
    )

    # The locked payables document(s) this event owns (direct links; the payables source GFK is
    # unindexed). `payment` is the module-created funding payment, kept for teardown.
    bill = models.OneToOneField(
        "payables.Bill", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="property_cost_event",
    )
    payment = models.ForeignKey(
        "payables.Payment", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    covers_from = models.DateField(null=True, blank=True)
    covers_through = models.DateField(null=True, blank=True)  # e.g. the tax/HOA period covered
    due_date = models.DateField(null=True, blank=True)  # → bill.due_date (payables aging)

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
                name="propertycostevent_one_vendor",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="propertycostevent_amount_positive"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.amount} on {self.date}"

    @_prop
    def kind_label(self) -> str:
        return self.get_kind_display()

    @_prop
    def kind_glyph(self) -> str:
        return COST_KIND_GLYPH.get(self.kind, "coins")

    @_prop
    def is_capitalizing(self) -> bool:
        return self.kind in CAPITALIZING_KINDS

    @_prop
    def vendor(self):
        return self.vendor_person or self.vendor_organization

    @_prop
    def vendor_kind(self) -> str:
        return "person" if self.vendor_person_id else "organization"

    @_prop
    def vendor_name(self) -> str:
        party = self.vendor
        if party is None:
            return ""
        for attr in ("display_name", "full_name", "name"):
            val = getattr(party, attr, "")
            if val:
                return val
        return str(party)

    @_prop
    def is_funded(self) -> bool:
        return self.funding_source in (Funding.BANK, Funding.CARD, Funding.CASH)

    # Duck-typed hooks read by the Payables locked-bill/payment back-link (module-agnostic there).
    @_prop
    def managed_label(self) -> str:
        return f"Property · {self.property.nickname}"

    @_prop
    def managed_url(self) -> str:
        return f"realestate/{self.property_id}/"


class PropertyValuation(TimeStampedModel):
    """A dated manual mark of a property's market value (twin of `automobile.VehicleValuation`). The
    latest on/before a date is the value shown; a display-only overlay that posts nothing to the GL.
    """

    property = models.ForeignKey(Property, on_delete=models.CASCADE, related_name="valuations")
    as_of = models.DateField()
    value = _money()
    source = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["-as_of"]
        constraints = [
            models.UniqueConstraint(fields=["property", "as_of"], name="propertyvaluation_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.property_id} @ {self.as_of}: {self.value}"


class PropertyDisposal(SoftDeleteModel):
    """The full disposal of a property — a direct finance entry (not a bill): proceeds vs book cost,
    the difference to a single gain/loss account (`4930`). Proceeds to a tracked bank account route
    via `1150` + a native banking TRANSFER_IN (`bank_txn`). The disposal does NOT touch the mortgage
    — record the mortgage payoff in the Loans module."""

    property = models.OneToOneField(
        Property, on_delete=models.CASCADE, related_name="disposal"
    )
    method = models.CharField(
        max_length=10, choices=DisposalMethod.choices, default=DisposalMethod.SALE
    )
    date = models.DateField()
    proceeds = _money(default=ZERO)

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
                name="propertydisposal_one_buyer",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_method_display()} — {self.property_id}"

    @_prop
    def method_label(self) -> str:
        return self.get_method_display()

    @_prop
    def gain_loss(self):
        """Proceeds − book cost (positive = gain). Once posted, read from the booked 4930 line so it
        stays correct after the property node is derecognized to zero; before posting it's a live
        estimate."""
        if self.journal_entry_id is not None:
            line = self.journal_entry.lines.filter(
                account__system_key="asset_disposal_gain_loss"
            ).first()
            return (line.base_credit - line.base_debit) if line is not None else ZERO
        return self.proceeds - self.property.cost

    @_prop
    def buyer(self):
        return self.buyer_person or self.buyer_organization
