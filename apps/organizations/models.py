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
    is_primary = models.BooleanField(default=False)
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
