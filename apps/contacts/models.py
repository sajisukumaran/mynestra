"""Contacts — the Person record and its life-event dates (DESIGN §5).

Person is soft-deletable and audited (simple-history). Real-world dates (birth, death, anniversary)
use the PartialDate pattern from apps.core.partialdate. Contact channels, addresses, important dates
and category links live in sibling modules / M2M added alongside.
"""

import datetime
import zlib

from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.models import SoftDeleteModel, TimeStampedModel
from apps.core.partialdate import PartialDate, partial_date_age

# Curated avatar tints (match .av-* in app.css); chosen deterministically from the display name.
AVATAR_TINTS = [
    "teal", "blue", "violet", "amber", "rose", "emerald", "sky", "cyan", "fuchsia", "slate",
]


class Person(SoftDeleteModel):
    class Gender(models.TextChoices):
        MALE = "M", "Male"
        FEMALE = "F", "Female"
        OTHER = "O", "Other"
        UNSPECIFIED = "U", "Prefer not to say"

    class Marital(models.TextChoices):
        SINGLE = "single", "Single"
        MARRIED = "married", "Married"
        WIDOWED = "widowed", "Widowed"
        DIVORCED = "divorced", "Divorced"
        SEPARATED = "separated", "Separated"
        PARTNERED = "partnered", "Partnered"

    class BloodGroup(models.TextChoices):
        A_POS = "A+", "A+"
        A_NEG = "A-", "A-"
        B_POS = "B+", "B+"
        B_NEG = "B-", "B-"
        AB_POS = "AB+", "AB+"
        AB_NEG = "AB-", "AB-"
        O_POS = "O+", "O+"
        O_NEG = "O-", "O-"
        UNKNOWN = "unknown", "Unknown"

    first_name = models.CharField(max_length=80)
    middle_name = models.CharField(max_length=80, blank=True)
    last_name = models.CharField(max_length=80)
    preferred_name = models.CharField(max_length=80, blank=True)
    pronouns = models.CharField(max_length=40, blank=True)
    gender = models.CharField(max_length=1, choices=Gender.choices, default=Gender.UNSPECIFIED)

    dob_year = models.SmallIntegerField(null=True, blank=True)
    dob_month = models.SmallIntegerField(null=True, blank=True)
    dob_day = models.SmallIntegerField(null=True, blank=True)

    is_deceased = models.BooleanField(default=False)
    dod_year = models.SmallIntegerField(null=True, blank=True)
    dod_month = models.SmallIntegerField(null=True, blank=True)
    dod_day = models.SmallIntegerField(null=True, blank=True)

    marital_status = models.CharField(max_length=16, choices=Marital.choices, blank=True)
    anniversary_year = models.SmallIntegerField(null=True, blank=True)
    anniversary_month = models.SmallIntegerField(null=True, blank=True)
    anniversary_day = models.SmallIntegerField(null=True, blank=True)

    occupation = models.CharField(max_length=120, blank=True)
    education = models.CharField(max_length=120, blank=True)
    blood_group = models.CharField(max_length=8, choices=BloodGroup.choices, blank=True)
    dietary = models.CharField(max_length=120, blank=True)
    languages = models.JSONField(default=list, blank=True)  # list[str]; simple + portable
    photo = models.ImageField(upload_to="person_photos/", null=True, blank=True)
    notes = models.TextField(blank=True)

    categories = models.ManyToManyField(
        "setup.Category", related_name="people", blank=True,
        limit_choices_to={"kind": "PERSON"},
    )

    history = HistoricalRecords()

    class Meta:
        ordering = ["first_name", "last_name"]
        verbose_name_plural = "people"

    def __str__(self) -> str:
        return self.display_name

    # --- derived display helpers ---
    @property
    def full_name(self) -> str:
        return " ".join(p for p in [self.first_name, self.middle_name, self.last_name] if p)

    @property
    def display_name(self) -> str:
        return self.preferred_name or f"{self.first_name} {self.last_name}".strip()

    @property
    def initials(self) -> str:
        a = self.first_name[:1]
        b = self.last_name[:1]
        return (a + b).upper() or "?"

    @property
    def avatar_tint(self) -> str:
        key = zlib.crc32(self.display_name.encode("utf-8"))
        return AVATAR_TINTS[key % len(AVATAR_TINTS)]

    @property
    def dob(self) -> PartialDate:
        return PartialDate.from_instance(self, "dob")

    @property
    def dod(self) -> PartialDate:
        return PartialDate.from_instance(self, "dod")

    @property
    def anniversary(self) -> PartialDate:
        return PartialDate.from_instance(self, "anniversary")

    @property
    def age(self) -> int | None:
        """Living age today; age at death when a year of death is known."""
        on = None
        if self.is_deceased and self.dod_year:
            on = datetime.date(self.dod_year, self.dod_month or 1, self.dod_day or 1)
        return partial_date_age(self.dob_year, self.dob_month, self.dob_day, on)

    @property
    def lifespan(self) -> str:
        """`1948 – 2019` style year range for a deceased person (blanks render as XXXX)."""
        start = f"{self.dob_year}" if self.dob_year else "XXXX"
        end = f"{self.dod_year}" if self.dod_year else "XXXX"
        return f"{start} – {end}"

    @property
    def list_subtitle(self) -> str:
        """Secondary line under the name in the People list (goes-by · age, or lifespan)."""
        if self.is_deceased:
            return self.lifespan
        parts = []
        if self.preferred_name:
            parts.append(self.preferred_name)
        if self.age is not None:
            parts.append(str(self.age))
        return " · ".join(parts)

    @property
    def primary_channel(self):
        return self.channels.filter(is_primary=True).first() or self.channels.first()

    @property
    def primary_city(self) -> str:
        addr = self.addresses.filter(is_primary=True).first() or self.addresses.first()
        return addr.city if addr else ""


# --- Unified contact info (DESIGN §5) -------------------------------------------------------
# ContactChannel and Address each attach to exactly ONE owner, enforced by a DB CHECK. The owner set
# grew per phase: `person` (P4) → `person | family` (P5) → the final four owners in P6.
OWNER_FIELDS = ("person", "family", "organization", "branch")


def _exactly_one_owner():
    """Q that holds iff exactly one owner FK is set — the CHECK on ContactChannel/Address."""
    q = models.Q()
    for chosen in OWNER_FIELDS:
        q |= models.Q(**{f"{f}__isnull": (f != chosen) for f in OWNER_FIELDS})
    return q


class ContactChannel(TimeStampedModel):
    class Type(models.TextChoices):
        PHONE = "phone", "Phone"
        EMAIL = "email", "Email"
        WHATSAPP = "whatsapp", "WhatsApp"
        URL = "url", "Website"
        OTHER = "other", "Other"

    _ICONS = {"phone": "phone", "email": "mail", "whatsapp": "message-circle", "url": "globe"}

    type = models.CharField(max_length=12, choices=Type.choices, default=Type.PHONE)
    label = models.CharField(max_length=40, blank=True)
    value = models.CharField(max_length=255)
    is_primary = models.BooleanField(default=False)
    person = models.ForeignKey(
        "contacts.Person", null=True, blank=True, on_delete=models.CASCADE, related_name="channels"
    )
    family = models.ForeignKey(
        "families.Family", null=True, blank=True, on_delete=models.CASCADE, related_name="channels"
    )
    organization = models.ForeignKey(
        "organizations.Organization", null=True, blank=True, on_delete=models.CASCADE,
        related_name="channels",
    )
    branch = models.ForeignKey(
        "organizations.Branch", null=True, blank=True, on_delete=models.CASCADE,
        related_name="channels",
    )
    history = HistoricalRecords()

    class Meta:
        ordering = ["-is_primary", "id"]
        constraints = [
            # Exactly one owner of the four (person | family | organization | branch).
            models.CheckConstraint(
                condition=_exactly_one_owner(), name="contactchannel_one_owner"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_type_display()}: {self.value}"

    @property
    def icon(self) -> str:
        return self._ICONS.get(self.type, "link")


class Address(TimeStampedModel):
    line1 = models.CharField(max_length=200, blank=True)
    line2 = models.CharField(max_length=200, blank=True)
    city = models.CharField(max_length=120, blank=True)
    region = models.CharField(max_length=120, blank=True)
    postal_code = models.CharField(max_length=32, blank=True)
    country = models.CharField(max_length=120, blank=True)
    label = models.CharField(max_length=40, blank=True)
    is_primary = models.BooleanField(default=False)
    person = models.ForeignKey(
        "contacts.Person", null=True, blank=True, on_delete=models.CASCADE, related_name="addresses"
    )
    family = models.ForeignKey(
        "families.Family", null=True, blank=True, on_delete=models.CASCADE, related_name="addresses"
    )
    organization = models.ForeignKey(
        "organizations.Organization", null=True, blank=True, on_delete=models.CASCADE,
        related_name="addresses",
    )
    branch = models.ForeignKey(
        "organizations.Branch", null=True, blank=True, on_delete=models.CASCADE,
        related_name="addresses",
    )
    history = HistoricalRecords()

    class Meta:
        ordering = ["-is_primary", "id"]
        verbose_name_plural = "addresses"
        constraints = [
            # Exactly one owner of the four (person | family | organization | branch).
            models.CheckConstraint(
                condition=_exactly_one_owner(), name="address_one_owner"
            ),
        ]

    def __str__(self) -> str:
        return self.one_line

    @property
    def one_line(self) -> str:
        parts = [self.line1, self.line2, self.city, self.region, self.postal_code, self.country]
        return ", ".join(p for p in parts if p)

    @property
    def locality(self) -> str:
        """`Bengaluru, Karnataka 560001 · India` — the secondary line on the detail card."""
        region_postal = " ".join(p for p in [self.region, self.postal_code] if p)
        city = f"{self.city}," if self.city and region_postal else self.city
        left = " ".join(p for p in [city, region_postal] if p)
        return f"{left} · {self.country}" if self.country and left else (left or self.country)


class ImportantDate(TimeStampedModel):
    """Extra dated events for a person (DESIGN §5). Birthday/anniversary live on Person; this holds
    the rest (e.g. Retirement). Date is a PartialDate (any part may be blank)."""

    person = models.ForeignKey(
        "contacts.Person", on_delete=models.CASCADE, related_name="important_dates"
    )
    label = models.CharField(max_length=80)
    date_year = models.SmallIntegerField(null=True, blank=True)
    date_month = models.SmallIntegerField(null=True, blank=True)
    date_day = models.SmallIntegerField(null=True, blank=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["date_month", "date_day", "id"]

    def __str__(self) -> str:
        return f"{self.label}: {self.date.display}"

    @property
    def date(self) -> PartialDate:
        return PartialDate.from_instance(self, "date")
