"""Organizations — companies/institutions a household deals with (DESIGN §5).

An Organization has any number of Identifiers (GST/Tax ID/…) and Branches; a Branch carries its own
contact channels/addresses (via the unified `contacts` owner FK, widened to `branch` in P6).
Person↔Organization links (P2O) live in `apps.relationships`. Soft-deletable + audited like every
tenant model. Screens compose the existing kit (no mockup — the Contacts/Setup idiom).
"""

import zlib

from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.models import SoftDeleteModel, TimeStampedModel
from apps.core.partialdate import PartialDate

# Curated avatar tints (match .av-* in app.css); chosen deterministically from the name.
ORG_TINTS = [
    "blue", "emerald", "violet", "teal", "amber", "rose", "sky", "cyan", "fuchsia", "slate",
]


class Organization(SoftDeleteModel):
    name = models.CharField(max_length=160)
    display_name = models.CharField(max_length=160, blank=True)
    logo = models.ImageField(upload_to="org_logos/", null=True, blank=True)
    website = models.URLField(blank=True)
    notes = models.TextField(blank=True)

    # Lifecycle (PartialDate): when the organization was established and, if it has closed, when.
    established_year = models.SmallIntegerField(null=True, blank=True)
    established_month = models.SmallIntegerField(null=True, blank=True)
    established_day = models.SmallIntegerField(null=True, blank=True)
    closed_year = models.SmallIntegerField(null=True, blank=True)
    closed_month = models.SmallIntegerField(null=True, blank=True)
    closed_day = models.SmallIntegerField(null=True, blank=True)

    categories = models.ManyToManyField(
        "setup.Category", related_name="organizations", blank=True,
        limit_choices_to={"kind": "ORG"},
    )

    history = HistoricalRecords()

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.display

    @property
    def display(self) -> str:
        return self.display_name or self.name

    @property
    def initials(self) -> str:
        parts = [p for p in self.display.split() if p]
        if not parts:
            return "?"
        return (parts[0][:1] + (parts[1][:1] if len(parts) > 1 else "")).upper()

    @property
    def avatar_tint(self) -> str:
        return ORG_TINTS[zlib.crc32(self.display.encode("utf-8")) % len(ORG_TINTS)]

    @property
    def primary_channel(self):
        return self.channels.filter(is_primary=True).first() or self.channels.first()

    @property
    def primary_city(self) -> str:
        addr = self.addresses.filter(is_primary=True).first() or self.addresses.first()
        return addr.city if addr else ""

    @property
    def established(self) -> PartialDate:
        return PartialDate.from_instance(self, "established")

    @property
    def closed(self) -> PartialDate:
        return PartialDate.from_instance(self, "closed")

    @property
    def is_closed(self) -> bool:
        return self.closed.is_set


class OrgIdentifier(TimeStampedModel):
    """A registration/tax identifier for an organization (e.g. GST, Tax ID). Many per org."""

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="identifiers"
    )
    type = models.CharField(max_length=60)
    value = models.CharField(max_length=120)
    history = HistoricalRecords()

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.type}: {self.value}"


class Branch(SoftDeleteModel):
    """A location/office of an organization. Carries its own channels/addresses (DESIGN §5)."""

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="branches"
    )
    name = models.CharField(max_length=160)
    number = models.CharField(max_length=40, blank=True)  # branch code / IFSC / internal number
    is_primary = models.BooleanField(default=False)

    # Lifecycle (PartialDate): when the branch opened and, if it has closed, when.
    opened_year = models.SmallIntegerField(null=True, blank=True)
    opened_month = models.SmallIntegerField(null=True, blank=True)
    opened_day = models.SmallIntegerField(null=True, blank=True)
    closed_year = models.SmallIntegerField(null=True, blank=True)
    closed_month = models.SmallIntegerField(null=True, blank=True)
    closed_day = models.SmallIntegerField(null=True, blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["-is_primary", "name"]
        verbose_name_plural = "branches"

    def __str__(self) -> str:
        return self.name

    @property
    def primary_city(self) -> str:
        addr = self.addresses.filter(is_primary=True).first() or self.addresses.first()
        return addr.city if addr else ""

    @property
    def primary_address(self):
        return self.addresses.filter(is_primary=True).first() or self.addresses.first()

    @property
    def opened(self) -> PartialDate:
        return PartialDate.from_instance(self, "opened")

    @property
    def closed(self) -> PartialDate:
        return PartialDate.from_instance(self, "closed")

    @property
    def is_closed(self) -> bool:
        return self.closed.is_set
