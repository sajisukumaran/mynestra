"""factory-boy factories. Use inside a tenant `schema_context` (contacts/families models are
per-tenant)."""

import factory

from apps.contacts.models import Person
from apps.families.models import Family, FamilyMembership
from apps.organizations.models import Branch, Organization


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


class OrganizationFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Organization

    name = factory.Sequence(lambda n: f"Org{n}")


class BranchFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Branch

    organization = factory.SubFactory(OrganizationFactory)
    name = factory.Sequence(lambda n: f"Branch{n}")
