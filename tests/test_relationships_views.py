"""P2P relationship views: create/edit/delete edges, dedup + self guards, htmx person search and
gender-aware label preview, and the Relationships tab rendering resolved labels."""

from django_tenants.utils import schema_context

from apps.contacts.models import Person
from apps.relationships.models import PersonRelationship, RelationshipType
from apps.tenants.models import Membership, Role


def _owner(make_tenant, make_user, name="Acme", email="owner@example.com"):
    tenant = make_tenant(name=name)
    owner = make_user(email)
    Membership.objects.create(user=owner, tenant=tenant, role=Role.OWNER)
    return tenant, owner


def _c(tenant, path=""):
    return f"/t/{tenant.schema_name}/contacts/{path}"


def _cast(make_tenant, make_user):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        raj = Person.objects.create(first_name="Rajesh", last_name="Sharma", gender="M")
        priya = Person.objects.create(first_name="Priya", last_name="Sharma", gender="F")
        aarav = Person.objects.create(first_name="Aarav", last_name="Sharma", gender="M")
        ids = {
            "raj": raj.pk, "priya": priya.pk, "aarav": aarav.pk,
            "spouse": RelationshipType.objects.get(code="spouse").pk,
            "parent_child": RelationshipType.objects.get(code="parent_child").pk,
        }
    return tenant, owner, ids


def test_relationship_create_resolves_on_both_pages(make_tenant, make_user, client):
    tenant, owner, ids = _cast(make_tenant, make_user)
    client.force_login(owner)
    # Rajesh + Priya spouse; Rajesh is parent of Aarav (Rajesh on the a-side).
    assert client.post(_c(tenant, f"people/{ids['raj']}/relationships/new/"),
                       {"other": ids["priya"], "typeside": f"{ids['spouse']}:a"}).status_code == 302
    client.post(_c(tenant, f"people/{ids['raj']}/relationships/new/"),
                {"other": ids["aarav"], "typeside": f"{ids['parent_child']}:a", "note": "eldest"})
    with schema_context(tenant.schema_name):
        assert PersonRelationship.objects.count() == 2

    raj_page = client.get(_c(tenant, f"people/{ids['raj']}/")).content.decode()
    assert "Priya Sharma" in raj_page and "Wife" in raj_page
    assert "Aarav Sharma" in raj_page and "Son" in raj_page
    # The reciprocal renders correctly from Aarav's page without a second stored row.
    aarav_page = client.get(_c(tenant, f"people/{ids['aarav']}/")).content.decode()
    assert "Rajesh Sharma" in aarav_page and "Father" in aarav_page


def test_relationship_dedup_ignores_repeat_and_reverse(make_tenant, make_user, client):
    tenant, owner, ids = _cast(make_tenant, make_user)
    client.force_login(owner)
    new_raj = _c(tenant, f"people/{ids['raj']}/relationships/new/")
    new_priya = _c(tenant, f"people/{ids['priya']}/relationships/new/")
    client.post(new_raj, {"other": ids["priya"], "typeside": f"{ids['spouse']}:a"})
    client.post(new_raj, {"other": ids["priya"], "typeside": f"{ids['spouse']}:a"})  # exact repeat
    client.post(new_priya, {"other": ids["raj"], "typeside": f"{ids['spouse']}:a"})  # reverse dir
    with schema_context(tenant.schema_name):
        assert PersonRelationship.objects.count() == 1  # stored once per unordered pair + type


def test_relationship_self_link_rejected(make_tenant, make_user, client):
    tenant, owner, ids = _cast(make_tenant, make_user)
    client.force_login(owner)
    client.post(_c(tenant, f"people/{ids['raj']}/relationships/new/"),
                {"other": ids["raj"], "typeside": f"{ids['spouse']}:a"})
    with schema_context(tenant.schema_name):
        assert PersonRelationship.objects.count() == 0


def test_relationship_search_excludes_self_and_linked(make_tenant, make_user, client):
    tenant, owner, ids = _cast(make_tenant, make_user)
    client.force_login(owner)
    client.post(_c(tenant, f"people/{ids['raj']}/relationships/new/"),
                {"other": ids["priya"], "typeside": f"{ids['spouse']}:a"})
    body = client.get(_c(tenant, f"people/{ids['raj']}/relationships/search/?q=")).content.decode()
    assert "Rajesh Sharma" not in body  # self excluded
    assert "Priya Sharma" not in body   # already linked
    assert "Aarav Sharma" in body       # still available


def test_relationship_preview_resolves_labels(make_tenant, make_user, client):
    tenant, owner, ids = _cast(make_tenant, make_user)
    client.force_login(owner)
    url = _c(tenant, f"people/{ids['raj']}/relationships/preview/")
    resp = client.get(url, {"other": ids["aarav"], "typeside": f"{ids['parent_child']}:a"})
    body = resp.content.decode()
    assert "Son" in body      # Aarav (b-side, male) will show as Son
    assert "Father" in body   # Rajesh (a-side, male) will show as Father


def test_relationship_edit_and_delete(make_tenant, make_user, client):
    tenant, owner, ids = _cast(make_tenant, make_user)
    client.force_login(owner)
    client.post(_c(tenant, f"people/{ids['raj']}/relationships/new/"),
                {"other": ids["aarav"], "typeside": f"{ids['parent_child']}:a"})
    with schema_context(tenant.schema_name):
        edge = PersonRelationship.objects.get()
    # Edit the note.
    client.post(_c(tenant, f"people/{ids['raj']}/relationships/{edge.pk}/edit/"),
                {"typeside": f"{ids['parent_child']}:a", "note": "updated"})
    with schema_context(tenant.schema_name):
        edge.refresh_from_db()
        assert edge.note == "updated"
    # Delete (soft) — leaves the People list untouched.
    client.post(_c(tenant, f"people/{ids['raj']}/relationships/{edge.pk}/delete/"))
    with schema_context(tenant.schema_name):
        assert PersonRelationship.objects.count() == 0
        assert Person.objects.count() == 3
