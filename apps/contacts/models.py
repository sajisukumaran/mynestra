"""Contacts — the Person record and its life-event dates (DESIGN §5).

Person is soft-deletable and audited (simple-history). Real-world dates (birth, death, anniversary)
use the PartialDate pattern from apps.core.partialdate. Contact channels, addresses, important dates
and category links live in sibling modules / M2M added alongside.
"""

import zlib

from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.models import SoftDeleteModel
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
            import datetime
            on = datetime.date(self.dod_year, self.dod_month or 1, self.dod_day or 1)
        return partial_date_age(self.dob_year, self.dob_month, self.dob_day, on)

    @property
    def lifespan(self) -> str:
        """`1948 – 2019` style year range for a deceased person (blanks render as XXXX)."""
        start = f"{self.dob_year}" if self.dob_year else "XXXX"
        end = f"{self.dod_year}" if self.dod_year else "XXXX"
        return f"{start} – {end}"
