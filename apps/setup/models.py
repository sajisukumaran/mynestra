"""Setup catalogs (tenant schema).

P1 introduces `Category` (the first tenant model) to satisfy §6 seeding. Per the approved P1
decision it carries only fields + `is_system` + timestamps; the soft-delete manager and
django-simple-history arrive in P4 with `Person`. The management UI + lock enforcement is P3.
"""

from django.db import models


class Category(models.Model):
    class Kind(models.TextChoices):
        PERSON = "PERSON", "Person"
        ORG = "ORG", "Organization"

    name = models.CharField(max_length=80)
    kind = models.CharField(max_length=6, choices=Kind.choices)
    # One of the curated category chip tints (DESIGN §7.1); independent of the household accent.
    color = models.CharField(max_length=16, default="slate")
    # Locked seed rows (§6): system categories cannot be edited/deleted (enforced in P3 UI).
    is_system = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "categories"
        ordering = ["kind", "name"]
        constraints = [
            models.UniqueConstraint(fields=["kind", "name"], name="uniq_category_kind_name"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.get_kind_display()})"
