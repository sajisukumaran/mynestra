"""Relationship types + edges (tenant schema).

The *type catalogs* (seeded §6) — the P2P `RelationshipType` and the P2O `PersonOrgRelationshipType`
— arrived in P1. P5 adds the P2P **edge** (`PersonRelationship`, stored once per unordered pair)
plus the gender-reciprocal label-resolution engine in `services.py`. The P2O edge
(`PersonOrgRelationship`) lands in P6 with the Organization model.
"""

from django.db import models
from django.db.models.functions import Greatest, Least
from simple_history.models import HistoricalRecords

from apps.core.models import SoftDeleteModel
from apps.relationships.services import label_for, other_side


class RelationshipType(models.Model):
    """Person-to-person relationship type with gender-aware labels for each side (DESIGN §5).

    A stored edge is `person_a`—`person_b`. To render the label describing a side-A person, use
    `a_label_<their gender>`; for a side-B person, `b_label_<their gender>`. Symmetric types use the
    same labels on both sides. The resolution logic itself lands in P5.
    """

    code = models.CharField(max_length=40, unique=True)
    is_symmetric = models.BooleanField(default=False)

    a_label_m = models.CharField(max_length=40)
    a_label_f = models.CharField(max_length=40)
    a_label_n = models.CharField(max_length=40)
    b_label_m = models.CharField(max_length=40)
    b_label_f = models.CharField(max_length=40)
    b_label_n = models.CharField(max_length=40)

    is_system = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["code"]

    def __str__(self) -> str:
        return self.code

    @property
    def display_name(self) -> str:
        """Human name for the *pairing* (used on relationship badges). Derived, not stored:
        symmetric types read as their neutral label ("Spouse"); asymmetric as "A–B"
        ("Parent–Child")."""
        if self.is_symmetric:
            return self.a_label_n
        return f"{self.a_label_n}–{self.b_label_n}"


class PersonOrgRelationshipType(models.Model):
    """Person-to-organization relationship type (DESIGN §5)."""

    code = models.CharField(max_length=40, unique=True)
    label = models.CharField(max_length=60)
    is_system = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["label"]

    def __str__(self) -> str:
        return self.label


class PersonRelationship(SoftDeleteModel):
    """A person-to-person relationship, **stored once per unordered pair + type** (DESIGN §5).

    `person_a`/`person_b` fix the direction for asymmetric types (a = the a-side, e.g. the parent);
    for symmetric types direction is meaningless and we canonicalise a = lower pk. The label shown
    for either endpoint is resolved from `type` + that endpoint's own gender + its side — see
    `label_for_person` / `label_for_other` and `services.label_for`.
    """

    person_a = models.ForeignKey(
        "contacts.Person", on_delete=models.CASCADE, related_name="rels_as_a"
    )
    person_b = models.ForeignKey(
        "contacts.Person", on_delete=models.CASCADE, related_name="rels_as_b"
    )
    type = models.ForeignKey(RelationshipType, on_delete=models.PROTECT, related_name="edges")
    note = models.CharField(max_length=200, blank=True)
    history = HistoricalRecords()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(person_a=models.F("person_b")),
                name="personrelationship_distinct_people",
            ),
            # "Stored once per unordered pair + type" — a functional unique on the sorted pair.
            # Partial (alive rows only) so a soft-deleted edge can be re-created later.
            models.UniqueConstraint(
                Least("person_a", "person_b"),
                Greatest("person_a", "person_b"),
                "type",
                condition=models.Q(deleted_at__isnull=True),
                name="personrelationship_unique_pair_type",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.person_a} — {self.person_b} ({self.type.code})"

    # --- label resolution (viewer-relative) -------------------------------------------------
    def side_of(self, person) -> str:
        return "a" if person.pk == self.person_a_id else "b"

    def other_of(self, person):
        return self.person_b if person.pk == self.person_a_id else self.person_a

    def label_for_person(self, person) -> str:
        """Label describing `person`'s own role in this edge (by their gender)."""
        return label_for(self.type, person.gender, self.side_of(person))

    def label_for_other(self, viewer) -> str:
        """Label describing the *other* endpoint, as shown on `viewer`'s page."""
        other = self.other_of(viewer)
        return label_for(self.type, other.gender, other_side(self.side_of(viewer)))
