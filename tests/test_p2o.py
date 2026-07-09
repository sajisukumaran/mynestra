"""Person↔Organization links (P2O): create/edit/delete from both the org and person sides,
(person, org, type) uniqueness, from/to PartialDates, and cross-surface visibility."""

from django_tenants.utils import schema_context

from apps.contacts.models import Person
from apps.organizations.models import Organization
from apps.relationships.models import PersonOrgRelationship, PersonOrgRelationshipType
from apps.tenants.models import Membership, Role


def _owner(make_tenant, make_user, name="Acme", email="owner@example.com"):
    tenant = make_tenant(name=name)
    owner = make_user(email)
    Membership.objects.create(user=owner, tenant=tenant, role=Role.OWNER)
    return tenant, owner


def _cast(make_tenant, make_user):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        raj = Person.objects.create(first_name="Rajesh", last_name="Sharma", gender="M")
        org = Organization.objects.create(name="HDFC Bank")
        ids = {
            "raj": raj.pk, "org": org.pk,
            "account_holder": PersonOrgRelationshipType.objects.get(code="account_holder").pk,
            "customer": PersonOrgRelationshipType.objects.get(code="customer").pk,
        }
    return tenant, owner, ids


def test_p2o_create_from_org_side_shows_on_both(make_tenant, make_user, client):
    tenant, owner, ids = _cast(make_tenant, make_user)
    client.force_login(owner)
    org_base = f"/t/{tenant.schema_name}/organizations/{ids['org']}/"
    person_base = f"/t/{tenant.schema_name}/contacts/people/{ids['raj']}/"

    client.post(org_base + "people/new/", {
        "person": ids["raj"], "type": ids["account_holder"],
        "from_year": "2009", "from_month": "", "from_day": "", "role_note": "Primary account",
    })
    with schema_context(tenant.schema_name):
        link = PersonOrgRelationship.objects.get()
        assert link.person_id == ids["raj"] and link.organization_id == ids["org"]
        assert link.from_date.display == "XX-XX-2009"

    # Shows on both surfaces: the org "Key people" tab and the person "Organizations" section.
    assert "Account Holder" in client.get(org_base).content.decode()
    assert "Account Holder" in client.get(person_base).content.decode()


def test_p2o_create_from_person_side(make_tenant, make_user, client):
    tenant, owner, ids = _cast(make_tenant, make_user)
    client.force_login(owner)
    client.post(f"/t/{tenant.schema_name}/contacts/people/{ids['raj']}/orgs/new/",
                {"organization": ids["org"], "type": ids["customer"]})
    with schema_context(tenant.schema_name):
        assert PersonOrgRelationship.objects.filter(
            person_id=ids["raj"], organization_id=ids["org"]
        ).count() == 1


def test_p2o_unique_per_person_org_type(make_tenant, make_user, client):
    tenant, owner, ids = _cast(make_tenant, make_user)
    client.force_login(owner)
    url = f"/t/{tenant.schema_name}/organizations/{ids['org']}/people/new/"
    client.post(url, {"person": ids["raj"], "type": ids["account_holder"]})
    client.post(url, {"person": ids["raj"], "type": ids["account_holder"]})  # duplicate
    with schema_context(tenant.schema_name):
        assert PersonOrgRelationship.objects.count() == 1
    # A different type between the same pair is allowed.
    client.post(url, {"person": ids["raj"], "type": ids["customer"]})
    with schema_context(tenant.schema_name):
        assert PersonOrgRelationship.objects.count() == 2


def test_p2o_edit_and_delete(make_tenant, make_user, client):
    tenant, owner, ids = _cast(make_tenant, make_user)
    client.force_login(owner)
    org_base = f"/t/{tenant.schema_name}/organizations/{ids['org']}/"
    client.post(org_base + "people/new/", {"person": ids["raj"], "type": ids["customer"]})
    with schema_context(tenant.schema_name):
        link = PersonOrgRelationship.objects.get()

    client.post(f"{org_base}people/{link.pk}/edit/",
                {"type": ids["account_holder"], "role_note": "updated", "from_year": "2010"})
    with schema_context(tenant.schema_name):
        link.refresh_from_db()
        assert link.type_id == ids["account_holder"] and link.role_note == "updated"
        assert link.from_year == 2010

    client.post(f"{org_base}people/{link.pk}/delete/")
    with schema_context(tenant.schema_name):
        assert PersonOrgRelationship.objects.count() == 0
        assert Person.objects.count() == 1 and Organization.objects.count() == 1
