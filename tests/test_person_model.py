"""Person model: soft-delete/restore, simple-history audit, display helpers."""

from django_tenants.utils import schema_context

from apps.contacts.models import AVATAR_TINTS, Person


def test_soft_delete_hides_but_all_objects_sees(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        p = Person.objects.create(first_name="Rajesh", last_name="Sharma")
        assert Person.objects.count() == 1

        p.delete()  # soft
        assert Person.objects.count() == 0
        assert Person.all_objects.count() == 1
        assert Person.all_objects.get(pk=p.pk).is_deleted

        p.restore()
        assert Person.objects.count() == 1
        assert not Person.objects.get(pk=p.pk).is_deleted


def test_hard_delete_removes(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        p = Person.objects.create(first_name="A", last_name="B")
        p.hard_delete()
        assert Person.all_objects.count() == 0


def test_history_records_create_and_update(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        p = Person.objects.create(first_name="A", last_name="B")
        p.occupation = "Architect"
        p.save()
        assert p.history.count() >= 2  # created + updated


def test_display_helpers(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        p = Person.objects.create(
            first_name="Rajesh", last_name="Sharma", preferred_name="Raj",
            dob_year=1974, dob_month=3, dob_day=14,
        )
        assert p.display_name == "Raj"
        assert p.full_name == "Rajesh Sharma"
        assert p.initials == "RS"
        assert p.dob.display == "14-Mar-1974"
        assert p.avatar_tint in AVATAR_TINTS


def test_deceased_lifespan_and_age(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        p = Person.objects.create(
            first_name="Mohan", last_name="Sharma", is_deceased=True,
            dob_year=1948, dod_year=2019,
        )
        assert p.lifespan == "1948 – 2019"
        assert p.age == 71  # age at death when a death year is known
