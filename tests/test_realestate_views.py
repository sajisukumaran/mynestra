"""Real Estate screens (authenticated tenant client): dashboard, list, property create (cash, with a
purchase that capitalizes), and recording a cost event through the locked-bill path."""

import datetime
from decimal import Decimal

from django_tenants.utils import schema_context

from apps.tenants.models import Membership, Role

D = Decimal


def _owner(make_tenant, make_user, name="RE Household", email="owner@re.test"):
    tenant = make_tenant(name=name)
    owner = make_user(email)
    Membership.objects.create(user=owner, tenant=tenant, role=Role.OWNER)
    return tenant, owner


def _org(name="Acme Realty"):
    from apps.organizations.models import Organization

    return Organization.objects.create(name=name)


def _usd():
    from apps.finance.models import Currency

    return Currency.objects.get(code="USD")


# --- screens render --------------------------------------------------------------------------

def test_dashboard_and_list_render(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        from apps.realestate.models import OwnershipMode, Property
        from apps.realestate.services import ensure_gl_account

        p = Property.objects.create(
            nickname="Lakeside Cottage", ownership_mode=OwnershipMode.OWNED_CASH, currency=_usd()
        )
        ensure_gl_account(p)
    client.force_login(owner)
    body = client.get(f"/t/{tenant.schema_name}/realestate/").content.decode()
    assert "Properties" in body and "Market value" in body
    lst = client.get(f"/t/{tenant.schema_name}/realestate/all/").content.decode()
    assert "Lakeside Cottage" in lst


def test_property_detail_and_form_render(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        from apps.realestate.models import CostKind, OwnershipMode, Property, PropertyCostEvent
        from apps.realestate.services import save_cost_event

        p = Property.objects.create(
            nickname="Family Home", ownership_mode=OwnershipMode.OWNED_CASH, currency=_usd()
        )
        ev = PropertyCostEvent(
            property=p, kind=CostKind.PURCHASE, date=datetime.date(2026, 1, 5),
            amount=D("300000"), vendor_organization=_org("Seller"),
        )
        ev.save()
        save_cost_event(ev, is_new=True)
        pid = p.pk
    client.force_login(owner)
    detail = client.get(f"/t/{tenant.schema_name}/realestate/{pid}/")
    assert detail.status_code == 200
    body = detail.content.decode()
    assert "Family Home" in body and "Cost register" in body
    assert client.get(f"/t/{tenant.schema_name}/realestate/new/").status_code == 200
    assert client.get(f"/t/{tenant.schema_name}/realestate/{pid}/edit/").status_code == 200


# --- create a cash property with a purchase --------------------------------------------------

def test_create_cash_property_with_purchase(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    resp = client.post(
        f"/t/{tenant.schema_name}/realestate/new/",
        {
            "nickname": "First Home", "property_type": "single_family",
            "use": "primary_residence", "ownership_mode": "owned_cash", "currency": "USD",
            "seller_organization": "", "seller_organization_new_name": "Acme Realty",
            "address_line1": "123 Main St", "city": "Springfield", "cost_basis": "",
            "purchase_price": "320000", "purchase_date": "2026-01-02", "purchase_funding": "none",
            "notes": "",
        },
    )
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        from apps.finance.services import account_balance
        from apps.realestate.models import Property

        p = Property.objects.get(nickname="First Home")
        assert p.gl_account is not None and p.gl_account.parent.code == "1410"
        assert account_balance(p.gl_account) == D("320000")  # purchase capitalized
        assert p.cost_events.count() == 1


# --- record a cost via the client ------------------------------------------------------------

def test_record_property_tax_cost(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        from apps.realestate.models import OwnershipMode, Property

        p = Property.objects.create(
            nickname="Family Home", ownership_mode=OwnershipMode.OWNED_CASH, currency=_usd()
        )
        pid = p.pk
    client.force_login(owner)
    resp = client.post(
        f"/t/{tenant.schema_name}/realestate/{pid}/costs/new/",
        {
            "kind": "property_tax", "date": "2026-02-01", "amount": "6000",
            "vendor_organization": "", "vendor_organization_new_name": "County Assessor",
            "funding_source": "none", "reference": "TAX-2026",
        },
    )
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        from apps.finance.services import account_balance
        from apps.realestate.models import PropertyCostEvent

        ev = PropertyCostEvent.objects.get(property_id=pid)
        assert ev.bill is not None and ev.bill.is_locked
        assert account_balance("property_tax_expense") == D("6000")
        assert account_balance("property_tax") == D("0")  # not the 5140 escrow home tax
