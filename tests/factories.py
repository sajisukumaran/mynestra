"""factory-boy factories. Use inside a tenant `schema_context` (contacts/families models are
per-tenant)."""

import factory

from apps.banking.models import BankAccount
from apps.contacts.models import Person
from apps.families.models import Family, FamilyMembership
from apps.finance.models import Currency
from apps.investments.models import InvestmentAccount, Security
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


class BankAccountFactory(factory.django.DjangoModelFactory):
    """Needs the finance catalogs seeded (USD currency); use inside a tenant schema_context."""

    class Meta:
        model = BankAccount

    bank = factory.SubFactory(OrganizationFactory)
    nickname = factory.Sequence(lambda n: f"Account{n}")
    account_type = "checking"
    currency = factory.LazyFunction(lambda: Currency.objects.get(code="USD"))


class InvestmentAccountFactory(factory.django.DjangoModelFactory):
    """Needs the finance catalogs seeded (USD currency); use inside a tenant schema_context.
    Call services.ensure_gl_account(acct) after building to provision the GL node."""

    class Meta:
        model = InvestmentAccount

    institution = factory.SubFactory(OrganizationFactory)
    nickname = factory.Sequence(lambda n: f"Portfolio{n}")
    registration = "taxable_individual"
    currency = factory.LazyFunction(lambda: Currency.objects.get(code="USD"))


class SecurityFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Security

    symbol = factory.Sequence(lambda n: f"SYM{n}")
    name = factory.Sequence(lambda n: f"Security {n}")
    kind = "stock"
    asset_class = "equity"
    currency = factory.LazyFunction(lambda: Currency.objects.get(code="USD"))
