"""factory-boy factories. Use inside a tenant `schema_context` (contacts models are per-tenant)."""

import factory

from apps.contacts.models import Person


class PersonFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Person

    first_name = factory.Sequence(lambda n: f"First{n}")
    last_name = factory.Sequence(lambda n: f"Last{n}")
