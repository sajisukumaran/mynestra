"""Automobile screens (authenticated tenant client): dashboard, list, create (with a purchase),
detail, cost register, the payables locked-bill/payment read-only guards, valuation, disposal, and
the live launcher tile."""

import datetime
from decimal import Decimal

from django_tenants.utils import schema_context

from apps.tenants.models import Membership, Role

D = Decimal


def _owner(make_tenant, make_user, name="Auto Household", email="owner@auto.test"):
    tenant = make_tenant(name=name)
    owner = make_user(email)
    Membership.objects.create(user=owner, tenant=tenant, role=Role.OWNER)
    return tenant, owner


def _org(name="Dealer"):
    from apps.organizations.models import Organization

    return Organization.objects.create(name=name)


def _usd():
    from apps.finance.models import Currency

    return Currency.objects.get(code="USD")


def _bank(nickname="Checking"):
    from apps.banking.models import AccountType as BAT
    from apps.banking.models import BankAccount
    from apps.banking.services import ensure_gl_account as bank_gl

    acct = BankAccount.objects.create(
        bank=_org("My Bank"), account_type=BAT.CHECKING, nickname=nickname, currency=_usd()
    )
    bank_gl(acct)
    return acct


def _vehicle(**kw):
    from apps.automobile.models import OwnershipMode, Vehicle

    defaults = {
        "nickname": "Family SUV", "ownership_mode": OwnershipMode.OWNED_CASH, "currency": _usd(),
    }
    defaults.update(kw)
    return Vehicle.objects.create(**defaults)


def _funded_cost(vehicle, kind, amount, bank):
    from apps.automobile.models import Funding, VehicleCostEvent
    from apps.automobile.services import save_cost_event

    ev = VehicleCostEvent(
        vehicle=vehicle, kind=kind, date=datetime.date(2026, 1, 15), amount=amount,
        vendor_organization=_org("Vendor"), funding_source=Funding.BANK, funding_account=bank,
    )
    ev.save()
    save_cost_event(ev, is_new=True)
    return ev


# --- screens render --------------------------------------------------------------------------

def test_dashboard_and_list_render(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        _vehicle()
    client.force_login(owner)
    body = client.get(f"/t/{tenant.schema_name}/automobile/").content.decode()
    assert "Fleet value" in body and "held at cost" in body
    lst = client.get(f"/t/{tenant.schema_name}/automobile/all/").content.decode()
    assert "Family SUV" in lst


def test_create_owned_cash_vehicle_with_purchase(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        bank = _bank()
        bank_id = bank.pk
    client.force_login(owner)
    resp = client.post(
        f"/t/{tenant.schema_name}/automobile/new/",
        {
            "nickname": "New Car", "ownership_mode": "owned_cash", "currency": "USD",
            "fuel_type": "gasoline", "mileage_unit": "mi",
            "dealer_organization_new_name": "City Motors", "dealer_organization": "",
            "insurer_organization": "", "insurer_organization_new_name": "",
            "purchase_price": "28000", "purchase_date": "2026-01-10", "initial_odometer": "12",
            "purchase_funding": "bank", "purchase_account": str(bank_id),
            "acquired_year": "2026", "acquired_month": "1", "acquired_day": "10",
        },
    )
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        from apps.automobile.models import CostKind, Vehicle
        from apps.finance.services import account_balance

        v = Vehicle.objects.get(nickname="New Car")
        assert v.gl_account is not None
        assert account_balance(v.gl_account) == D("28000")
        ev = v.cost_events.get(kind=CostKind.PURCHASE)
        assert ev.bill is not None and ev.bill.is_locked and ev.bill.status == "paid"
        assert v.dealer_organization is not None  # inline-created dealer
        assert account_balance("accounts_payable") == D("0")


def test_detail_renders_with_costs(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        from apps.automobile.models import CostKind

        v = _vehicle()
        bank = _bank()
        _funded_cost(v, CostKind.FUEL, D("55"), bank)
        vid = v.pk
    client.force_login(owner)
    body = client.get(f"/t/{tenant.schema_name}/automobile/{vid}/").content.decode()
    assert "Family SUV" in body
    assert "Cost register" in body
    assert "BILL-" in body  # the locked-bill badge links to payables


# --- payables read-only guards (the lock seam) ----------------------------------------------

def test_locked_bill_and_payment_are_readonly_in_payables(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        from apps.automobile.models import CostKind

        v = _vehicle()
        bank = _bank()
        ev = _funded_cost(v, CostKind.INSURANCE, D("900"), bank)
        bill_id, pay_id = ev.bill_id, ev.payment_id
    client.force_login(owner)
    # The bill edit view refuses a locked bill.
    assert client.get(f"/t/{tenant.schema_name}/payables/bills/{bill_id}/edit/").status_code == 403
    # The payment edit + delete views refuse a locked payment.
    assert (
        client.get(f"/t/{tenant.schema_name}/payables/payments/{pay_id}/edit/").status_code == 403
    )
    assert (
        client.post(f"/t/{tenant.schema_name}/payables/payments/{pay_id}/delete/").status_code
        == 403
    )
    # The bill detail names the owning module and links back to the vehicle.
    detail = client.get(f"/t/{tenant.schema_name}/payables/bills/{bill_id}/").content.decode()
    assert "Managed elsewhere" in detail
    assert f"automobile/{v.pk}/" in detail


# --- valuation + disposal via the client ----------------------------------------------------

def test_valuation_and_disposal_via_client(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        from apps.automobile.models import CostKind

        v = _vehicle()
        bank = _bank()
        _funded_cost(v, CostKind.PURCHASE, D("30000"), bank)
        vid = v.pk
    client.force_login(owner)
    # A manual valuation posts nothing but moves current_value.
    client.post(
        f"/t/{tenant.schema_name}/automobile/{vid}/valuation/",
        {"value": "26000", "as_of": "2026-06-01", "source": "KBB"},
    )
    # Dispose (sale to cash).
    resp = client.post(
        f"/t/{tenant.schema_name}/automobile/{vid}/dispose/",
        {"method": "sale", "date": "2026-07-01", "proceeds": "24000"},
    )
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        from apps.automobile.models import Vehicle
        from apps.finance.services import account_balance

        v = Vehicle.objects.get(pk=vid)
        assert v.current_value == D("26000")
        assert v.is_active is False and hasattr(v, "disposal")
        assert account_balance(v.gl_account) == D("0")  # node derecognized
        assert v.disposal.gain_loss == D("-6000")       # 24000 − 30000


def test_launcher_tile_is_live(make_tenant, make_user, client):
    from django.apps import apps as django_apps

    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        _vehicle()
        counts = {
            c["label"]: c["n"]
            for c in django_apps.get_app_config("automobile").launcher_counts()
        }
    assert counts["Vehicles"] == 1
    client.force_login(owner)
    body = client.get(f"/t/{tenant.schema_name}/").content.decode()
    assert "Vehicles" in body and "automobile/" in body  # live tile links to the app
