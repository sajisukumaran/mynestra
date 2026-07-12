"""Correcting a mistaken manually-entered security price: edit (value / date / source), delete, and
editing onto a date another mark already holds (overwrite + drop). Prices are pure market-value
marks — no GL / lot effect."""

import datetime
from decimal import Decimal

from django_tenants.utils import schema_context

from apps.finance.models import Currency
from apps.investments.models import Security, SecurityPrice
from apps.tenants.models import Membership, Role

D = Decimal


def _owner(make_tenant, make_user):
    tenant = make_tenant(name="Portfolios")
    owner = make_user("owner@example.com")
    Membership.objects.create(user=owner, tenant=tenant, role=Role.OWNER)
    return tenant, owner


def _url(tenant, path=""):
    return f"/t/{tenant.schema_name}/investments/{path}"


def _sec():
    return Security.objects.create(
        symbol="AAPL", name="Apple", currency=Currency.objects.get(code="USD"))


def test_edit_price_corrects_value_date_and_source(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        sec = _sec()
        p = SecurityPrice.objects.create(
            security=sec, as_of=datetime.date(2026, 7, 10), price=D("999"))
    client.force_login(owner)
    resp = client.post(
        _url(tenant, f"securities/{sec.pk}/price/{p.pk}/edit/"),
        {"price": "214.30", "as_of": "2026-07-09", "source": "corrected"},
    )
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        p.refresh_from_db()
        assert p.price == D("214.30")
        assert p.as_of == datetime.date(2026, 7, 9)
        assert p.source == "corrected"


def test_delete_price(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        sec = _sec()
        p = SecurityPrice.objects.create(
            security=sec, as_of=datetime.date(2026, 7, 10), price=D("999"))
    client.force_login(owner)
    resp = client.post(_url(tenant, f"securities/{sec.pk}/price/{p.pk}/delete/"))
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        assert not SecurityPrice.objects.filter(pk=p.pk).exists()


def test_edit_onto_existing_date_overwrites_and_drops_row(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        sec = _sec()
        keep = SecurityPrice.objects.create(
            security=sec, as_of=datetime.date(2026, 7, 9), price=D("100"))
        dup = SecurityPrice.objects.create(
            security=sec, as_of=datetime.date(2026, 7, 10), price=D("200"))
    client.force_login(owner)
    # Move `dup` onto the 9th, which `keep` already holds → overwrite keep, delete dup.
    client.post(
        _url(tenant, f"securities/{sec.pk}/price/{dup.pk}/edit/"),
        {"price": "150", "as_of": "2026-07-09", "source": ""},
    )
    with schema_context(tenant.schema_name):
        assert SecurityPrice.objects.filter(security=sec).count() == 1
        keep.refresh_from_db()
        assert keep.as_of == datetime.date(2026, 7, 9)
        assert keep.price == D("150")  # overwritten with the edited value
        assert not SecurityPrice.objects.filter(pk=dup.pk).exists()


def test_security_detail_renders_price_edit_control(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        sec = _sec()
        p = SecurityPrice.objects.create(
            security=sec, as_of=datetime.date(2026, 7, 10), price=D("214.30"))
    client.force_login(owner)
    body = client.get(_url(tenant, f"securities/{sec.pk}/")).content.decode()
    assert "Edit price" in body
    assert f"price/{p.pk}/edit/" in body
    assert f"price/{p.pk}/delete/" in body
