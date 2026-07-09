"""P2P relationship label resolution (DESIGN §5, §10).

A stored `PersonRelationship` edge is `person_a`—`person_b` of a given `RelationshipType`. The label
shown for a related person Y **describes Y, indexed by Y's own gender**, drawn from the a-/b-side
label set for the side Y sits on. Symmetric types carry identical a/b labels, so side is irrelevant
for them. (The one place this matters: a stored parent→child edge renders "Father"/"Mother" for the
parent and "Son"/"Daughter" for the child, each by that person's own gender — never the viewer's.)
"""

# Person.gender (M/F/O/U) → the label-suffix on RelationshipType (m/f/n). O and U resolve neutral.
GENDER_KEY = {"M": "m", "F": "f", "O": "n", "U": "n"}


def label_for(rel_type, gender, side):
    """Label describing a person of `gender` sitting on `side` ('a' | 'b') of `rel_type`."""
    return getattr(rel_type, f"{side}_label_{GENDER_KEY.get(gender, 'n')}")


def other_side(side):
    return "b" if side == "a" else "a"


def parse_partial_dates(post, *prefixes):
    """Pull `<prefix>_year/_month/_day` ints from a POST (blank → None). Used by the P2O modal on
    both the org and person sides to read the `from`/`to` PartialDate parts."""
    def geti(name):
        v = (post.get(name) or "").strip()
        return int(v) if v.lstrip("-").isdigit() else None

    return {
        f"{prefix}_{part}": geti(f"{prefix}_{part}")
        for prefix in prefixes
        for part in ("year", "month", "day")
    }
