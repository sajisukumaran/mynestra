"""factory-boy factories. Use inside a tenant `schema_context` (contacts/families models are
per-tenant)."""

import factory

from apps.contacts.models import Person
from apps.families.models import Family, FamilyMembership


class PersonFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Person

    first_name = factory.Sequence(lambda n: f"First{n}")
    last_name = factory.Sequence(lambda n: f"Last{n}")


class FamilyFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Family

    name = factory.Sequence(lambda n: f"Family{n}")


class FamilyMembershipFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = FamilyMembership

    family = factory.SubFactory(FamilyFactory)
    person = factory.SubFactory(PersonFactory)
