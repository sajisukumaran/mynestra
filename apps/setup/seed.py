"""System category seed (§6). Idempotent; run inside the target tenant's schema_context."""

from .models import Category

# (name, chip tint) — tints from the curated set in DESIGN §7.1.
ORG_CATEGORIES = [
    ("Bank", "blue"),
    ("Brokerage", "sky"),
    ("Hospital/Clinic", "rose"),
    ("Pharmacy", "emerald"),
    ("School/College", "amber"),
    ("Insurance", "violet"),
    ("Government", "slate"),
    ("Employer", "teal"),
    ("Utility", "sky"),
    ("Merchant/Store", "orange"),
    ("Vendor", "amber"),
    ("Religious", "fuchsia"),
    ("Club/Association", "blue"),
]

PERSON_CATEGORIES = [
    ("Doctor", "rose"),
    ("Lawyer", "slate"),
    ("Accountant", "emerald"),
    ("Agent/Advisor", "amber"),
    ("Teacher", "sky"),
    ("Household Help", "teal"),
    ("Friend of family", "violet"),
]


def seed_categories() -> None:
    for name, color in ORG_CATEGORIES:
        Category.objects.update_or_create(
            kind=Category.Kind.ORG,
            name=name,
            defaults={"color": color, "is_system": True},
        )
    for name, color in PERSON_CATEGORIES:
        Category.objects.update_or_create(
            kind=Category.Kind.PERSON,
            name=name,
            defaults={"color": color, "is_system": True},
        )
