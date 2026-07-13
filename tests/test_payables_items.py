"""Payables item/SKU catalog: CRUD, UPC/SKU search, SKU management, latest_price, soft-delete."""

from decimal import Decimal

from django_tenants.utils import schema_context

from apps.payables.models import Item, ItemSku
from apps.tenants.models import Membership, Role


def _member(make_tenant, make_user, name="Acme", email="m@example.com"):
    tenant = make_tenant(name=name)
    user = make_user(email)
    Membership.objects.create(user=user, tenant=tenant, role=Role.MEMBER)
    return tenant, user


def _u(tenant, path=""):
    return f"/t/{tenant.schema_name}/payables/{path}"


def test_item_create_and_edit(make_tenant, make_user, client):
    tenant, user = _member(make_tenant, make_user)
    client.force_login(user)
    resp = client.post(_u(tenant, "items/new/"), {
        "name": "Laptop", "kind": "good", "unit": "each", "upc": "012345678905",
        "description": "", "notes": "", "is_active": "on", "capitalize_default": "on",
    })
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        item = Item.objects.get(name="Laptop")
        assert item.kind == "good" and item.upc == "012345678905"
        assert item.capitalize_default is True

    resp = client.post(_u(tenant, f"items/{item.pk}/edit/"), {
        "name": "Laptop Pro", "kind": "good", "unit": "each", "upc": "012345678905",
        "description": "", "notes": "", "is_active": "on",  # capitalize omitted -> cleared
    })
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        item.refresh_from_db()
        assert item.name == "Laptop Pro" and item.capitalize_default is False


def test_item_search_by_name_upc_and_sku(make_tenant, make_user, client):
    tenant, user = _member(make_tenant, make_user)
    client.force_login(user)
    with schema_context(tenant.schema_name):
        milk = Item.objects.create(name="Milk", kind="good", upc="99999")
        ItemSku.objects.create(item=milk, store_name="Amazon", sku="AMZ-MILK-1",
                               last_price=Decimal("3.49"))
        Item.objects.create(name="Bread", kind="good")

    assert b"Milk" in client.get(_u(tenant, "items/?q=milk")).content
    by_upc = client.get(_u(tenant, "items/?q=99999")).content
    assert b"Milk" in by_upc and b"Bread" not in by_upc
    by_sku = client.get(_u(tenant, "items/?q=AMZ-MILK")).content
    assert b"Milk" in by_sku and b"Bread" not in by_sku
    assert client.get(_u(tenant, "items/?kind=service")).status_code == 200


def test_sku_lifecycle_and_latest_price(make_tenant, make_user, client):
    tenant, user = _member(make_tenant, make_user)
    client.force_login(user)
    with schema_context(tenant.schema_name):
        item = Item.objects.create(name="Coffee", kind="good")

    assert client.post(_u(tenant, f"items/{item.pk}/skus/new/"),
                       {"store_name": "Costco", "sku": "CST-COF",
                        "last_price": "12.99", "note": ""}).status_code == 302
    with schema_context(tenant.schema_name):
        sku = ItemSku.objects.get(item=item, sku="CST-COF")
        assert sku.last_price == Decimal("12.99")
        item.refresh_from_db()
        assert item.latest_price == Decimal("12.99")

    assert client.post(_u(tenant, f"items/{item.pk}/skus/{sku.pk}/edit/"),
                       {"store_name": "Costco", "sku": "CST-COF",
                        "last_price": "10.99", "note": "sale"}).status_code == 302
    with schema_context(tenant.schema_name):
        sku.refresh_from_db()
        assert sku.last_price == Decimal("10.99") and sku.note == "sale"

    assert client.post(_u(tenant, f"items/{item.pk}/skus/{sku.pk}/delete/")).status_code == 302
    with schema_context(tenant.schema_name):
        assert not ItemSku.objects.filter(pk=sku.pk).exists()


def test_item_soft_delete(make_tenant, make_user, client):
    tenant, user = _member(make_tenant, make_user)
    client.force_login(user)
    with schema_context(tenant.schema_name):
        item = Item.objects.create(name="Widget", kind="good")
    assert client.post(_u(tenant, f"items/{item.pk}/delete/")).status_code == 302
    with schema_context(tenant.schema_name):
        assert not Item.objects.filter(pk=item.pk).exists()      # hidden by default manager
        assert Item.all_objects.filter(pk=item.pk).exists()      # soft-deleted, not gone


def test_item_search_fragment(make_tenant, make_user, client):
    tenant, user = _member(make_tenant, make_user)
    client.force_login(user)
    with schema_context(tenant.schema_name):
        Item.objects.create(name="Notebook", kind="good", upc="55555")
    r = client.get(_u(tenant, "item-search/?q=note"))
    assert r.status_code == 200 and b"Notebook" in r.content
