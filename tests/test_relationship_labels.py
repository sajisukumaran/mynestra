"""P2P label-resolution matrix (DESIGN §10).

For every seeded relationship type, every gender pairing, and each side, assert the engine renders
the label that describes each endpoint by *its own* gender — a stored edge resolves correctly from
both people's pages (parent→child shows Father/Son AND Son sees Father). The oracle is the type's
stored label fields, read independently of the engine's side/gender-key selection.
"""

from django_tenants.utils import schema_context

from apps.contacts.models import Person
from apps.relationships.models import PersonRelationship, RelationshipType
from apps.relationships.services import GENDER_KEY

GENDERS = ["M", "F", "O", "U"]


def test_label_resolution_matrix(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        # Two disjoint people per gender so every (a_gender, b_gender) pair is a distinct pair.
        def mk(prefix):
            return {
                g: Person.objects.create(first_name=f"{prefix}{g}", last_name="Side", gender=g)
                for g in GENDERS
            }

        a, b = mk("A"), mk("B")

        types = list(RelationshipType.objects.all())
        assert len(types) == 11  # the full seeded §6 P2P catalog

        for t in types:
            for ag in GENDERS:
                for bg in GENDERS:
                    pa, pb = a[ag], b[bg]
                    edge = PersonRelationship.objects.create(person_a=pa, person_b=pb, type=t)
                    exp_a = getattr(t, f"a_label_{GENDER_KEY[ag]}")  # describes the a-side person
                    exp_b = getattr(t, f"b_label_{GENDER_KEY[bg]}")  # describes the b-side person
                    # The label shown for the *other* endpoint (viewer-relative).
                    assert edge.label_for_other(pa) == exp_b
                    assert edge.label_for_other(pb) == exp_a
                    # The label describing an endpoint's own role.
                    assert edge.label_for_person(pa) == exp_a
                    assert edge.label_for_person(pb) == exp_b


def test_label_resolution_spot_checks(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        pc = RelationshipType.objects.get(code="parent_child")
        sp = RelationshipType.objects.get(code="spouse")

        # Stored parent (a-side) → child (b-side): both pages resolve by each person's gender.
        dad = Person.objects.create(first_name="Dad", last_name="X", gender="M")
        son = Person.objects.create(first_name="Son", last_name="X", gender="M")
        e = PersonRelationship.objects.create(person_a=dad, person_b=son, type=pc)
        assert e.label_for_other(dad) == "Son"
        assert e.label_for_other(son) == "Father"

        mom = Person.objects.create(first_name="Mom", last_name="Y", gender="F")
        dau = Person.objects.create(first_name="Dau", last_name="Y", gender="F")
        e2 = PersonRelationship.objects.create(person_a=mom, person_b=dau, type=pc)
        assert e2.label_for_other(mom) == "Daughter"
        assert e2.label_for_other(dau) == "Mother"

        # O / U resolve to the neutral label.
        nb = Person.objects.create(first_name="Nb", last_name="Z", gender="O")
        kid = Person.objects.create(first_name="Kid", last_name="Z", gender="U")
        e3 = PersonRelationship.objects.create(person_a=nb, person_b=kid, type=pc)
        assert e3.label_for_other(nb) == "Child"
        assert e3.label_for_other(kid) == "Parent"

        # Symmetric type: each side renders by its own gender, both ways.
        husband = Person.objects.create(first_name="H", last_name="S", gender="M")
        wife = Person.objects.create(first_name="W", last_name="S", gender="F")
        e4 = PersonRelationship.objects.create(person_a=husband, person_b=wife, type=sp)
        assert e4.label_for_other(husband) == "Wife"
        assert e4.label_for_other(wife) == "Husband"

        # Derived pairing badge label.
        assert sp.display_name == "Spouse"
        assert pc.display_name == "Parent–Child"
