"""Relationship type catalogs (tenant schema).

P1 introduces only the *type catalogs* (seeded §6) — the P2P `RelationshipType` and the P2O
`PersonOrgRelationshipType`. The `PersonRelationship`/`PersonOrgRelationship` edges and the
gender-reciprocal label-resolution engine are P5. Fields + `is_system` + timestamps only for now
(the soft-delete/history base arrives in P4).
"""

from django.db import models


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
