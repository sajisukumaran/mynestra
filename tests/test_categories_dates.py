"""Person↔Category M2M (person-kind) and ImportantDate (PartialDate)."""

from django_tenants.utils import schema_context

from apps.contacts.models import ImportantDate, Person
from apps.setup.models import Category


def test_person_categories_m2m(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        p = Person.objects.create(first_name="Rajesh", last_name="Sharma")
        cat = Category.objects.filter(kind=Category.Kind.PERSON).first()  # seeded §6
        p.categories.add(cat)
        assert p.categories.count() == 1
        assert cat.people.filter(pk=p.pk).exists()  # reverse accessor for filter-chip counts


def test_important_date_partial(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        p = Person.objects.create(first_name="A", last_name="B")
        d = ImportantDate.objects.create(person=p, label="Retirement", date_month=3, date_year=2039)
        assert p.important_dates.count() == 1
        assert d.date.display == "XX-Mar-2039"
