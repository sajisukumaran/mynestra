"""Families — a named household or circle a person belongs to (DESIGN §5).

A Family has no member roles: membership is a plain link, and the family page derives the
interpersonal relationships between its members from `relationships.PersonRelationship` rather than
storing them. A Family may own an Address via the unified `contacts.Address.family` FK (added when
that owner is widened in P5). Soft-deletable + audited like every tenant model.
"""

import zlib

from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.models import SoftDeleteModel, TimeStampedModel

# Curated avatar tints (match .av-* in app.css); chosen deterministically from the name.
FAMILY_TINTS = [
    "teal", "violet", "blue", "emerald", "amber", "rose", "sky", "cyan", "fuchsia", "slate",
]


class Family(SoftDeleteModel):
    name = models.CharField(max_length=120)
    photo = models.ImageField(upload_to="family_photos/", null=True, blank=True)
    notes = models.TextField(blank=True)

    members = models.ManyToManyField(
        "contacts.Person", through="families.FamilyMembership", related_name="families", blank=True
    )

    history = HistoricalRecords()

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "families"

    def __str__(self) -> str:
        return self.name

    @property
    def member_count(self) -> int:
        return self.memberships.count()

    @property
    def initials(self) -> str:
        parts = [p for p in self.name.split() if p]
        if not parts:
            return "?"
        letters = parts[0][:1] + (parts[1][:1] if len(parts) > 1 else "")
        return letters.upper()

    @property
    def avatar_tint(self) -> str:
        return FAMILY_TINTS[zlib.crc32(self.name.encode("utf-8")) % len(FAMILY_TINTS)]

    @property
    def primary_address(self):
        return self.addresses.filter(is_primary=True).first() or self.addresses.first()


class FamilyMembership(TimeStampedModel):
    """A person's membership in a family. No role (DESIGN §5); a person may be in many families."""

    family = models.ForeignKey(Family, on_delete=models.CASCADE, related_name="memberships")
    person = models.ForeignKey(
        "contacts.Person", on_delete=models.CASCADE, related_name="family_memberships"
    )
    history = HistoricalRecords()

    class Meta:
        ordering = ["person__first_name", "person__last_name"]
        constraints = [
            models.UniqueConstraint(fields=["family", "person"], name="familymembership_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.person} ∈ {self.family}"
