"""System relationship-type seed (§6). Idempotent; run inside the target tenant's schema_context."""

from .models import PersonOrgRelationshipType, RelationshipType

# (code, is_symmetric, (a_m, a_f, a_n), (b_m, b_f, b_n))
P2P_TYPES = [
    ("parent_child", False, ("Father", "Mother", "Parent"), ("Son", "Daughter", "Child")),
    ("spouse", True, ("Husband", "Wife", "Spouse"), ("Husband", "Wife", "Spouse")),
    ("sibling", True, ("Brother", "Sister", "Sibling"), ("Brother", "Sister", "Sibling")),
    (
        "grandparent_grandchild",
        False,
        ("Grandfather", "Grandmother", "Grandparent"),
        ("Grandson", "Granddaughter", "Grandchild"),
    ),
    (
        "uncle_aunt_nephew_niece",
        False,
        ("Uncle", "Aunt", "Aunt/Uncle"),
        ("Nephew", "Niece", "Niece/Nephew"),
    ),
    ("cousin", True, ("Cousin", "Cousin", "Cousin"), ("Cousin", "Cousin", "Cousin")),
    (
        "parent_in_law_child_in_law",
        False,
        ("Father-in-law", "Mother-in-law", "Parent-in-law"),
        ("Son-in-law", "Daughter-in-law", "Child-in-law"),
    ),
    (
        "sibling_in_law",
        True,
        ("Brother-in-law", "Sister-in-law", "Sibling-in-law"),
        ("Brother-in-law", "Sister-in-law", "Sibling-in-law"),
    ),
    ("friend", True, ("Friend", "Friend", "Friend"), ("Friend", "Friend", "Friend")),
    (
        "colleague",
        True,
        ("Colleague", "Colleague", "Colleague"),
        ("Colleague", "Colleague", "Colleague"),
    ),
    (
        "neighbour",
        True,
        ("Neighbour", "Neighbour", "Neighbour"),
        ("Neighbour", "Neighbour", "Neighbour"),
    ),
]

# (code, label)
P2O_TYPES = [
    ("customer", "Customer"),
    ("account_holder", "Account Holder"),
    ("employee", "Employee"),
    ("patient", "Patient"),
    ("student", "Student"),
    ("member", "Member"),
    ("owner", "Owner"),
    ("service_provider_contact", "Service-Provider Contact"),
]


def seed_relationship_types() -> None:
    for code, is_symmetric, a, b in P2P_TYPES:
        RelationshipType.objects.update_or_create(
            code=code,
            defaults={
                "is_symmetric": is_symmetric,
                "a_label_m": a[0], "a_label_f": a[1], "a_label_n": a[2],
                "b_label_m": b[0], "b_label_f": b[1], "b_label_n": b[2],
                "is_system": True,
            },
        )
    for code, label in P2O_TYPES:
        PersonOrgRelationshipType.objects.update_or_create(
            code=code,
            defaults={"label": label, "is_system": True},
        )
