"""Payables bill screens: create (posts on save + inline vendor), in-place edit, void, locked-bill
guard, list, and detail."""

import datetime
from decimal import Decimal

from django_tenants.utils import schema_context

from apps.finance.models import JournalEntry
from apps.finance.services import account_balance
from apps.organizations.models import Organization
from apps.payables.models import Bill
from apps.payables.services import post_bill
from apps.tenants.models import Membership, Role

D = Decimal


def _member(make_tenant, make_user, name="Acme HH", email="m@example.com"):
    tenant = make_tenant(name=name)
    user = make_user(email)
    Membership.objects.create(user=user, tenant=tenant, role=Role.MEMBER)
    return tenant, user


def _u(tenant, path=""):
    return f"/t/{tenant.schema_name}/payables/{path}"


def _create_data(unit_price="50", **over):
    data = {
        "new_vendor_name": "Acme Inc", "party_kind": "", "party_id": "",
        "bill_date": "2026-02-01", "vendor_ref": "INV-9", "terms": "", "due_date": "",
        "currency": "", "notes": "",
        "store_name": "", "order_number": "", "order_date": "", "carrier": "",
        "tracking_number": "", "ship_date": "", "delivery_date": "",
        "line_type": ["expense", "tax"],
        "line_item": ["", ""],
        "line_description": ["Widgets", "Sales tax"],
        "line_quantity": ["2", "1"],
        "line_unit_price": [unit_price, "8"],
        "line_discount": ["0", "0"],
        "line_tax": ["0", "0"],
        "line_account": ["", ""],
        "line_capitalize": ["0", "0"],
        "line_asset_serial": ["", ""],
        "line_warranty_end": ["", ""],
    }
    data.update(over)
    return data


def test_bill_create_posts_and_tags_vendor(make_tenant, make_user, client):
    tenant, user = _member(make_tenant, make_user)
    client.force_login(user)
    resp = client.post(_u(tenant, "bills/new/"), _create_data())
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        bill = Bill.objects.get()
        assert bill.total == D("108")  # 100 expense + 8 tax
        assert bill.journal_entry_id is not None
        assert account_balance("accounts_payable") == D("108")
        assert account_balance("5900") == D("100")       # expense default fallback
        assert account_balance("sales_tax_paid") == D("8")
        org = Organization.objects.get(name="Acme Inc")
        assert org.categories.filter(kind="ORG", name="Vendor").exists()
    listing = client.get(_u(tenant, "bills/")).content
    assert bill.bill_number.encode() in listing


def test_bill_edit_in_place_no_new_entry(make_tenant, make_user, client):
    tenant, user = _member(make_tenant, make_user)
    client.force_login(user)
    client.post(_u(tenant, "bills/new/"), _create_data())
    with schema_context(tenant.schema_name):
        bill = Bill.objects.get()
        je_before = JournalEntry.objects.count()

    resp = client.post(_u(tenant, f"bills/{bill.pk}/edit/"), _create_data(unit_price="60"))
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        assert JournalEntry.objects.count() == je_before   # in-place: no reversal, no new entry
        assert account_balance("accounts_payable") == D("128")  # 120 + 8


def test_bill_void_reverses(make_tenant, make_user, client):
    tenant, user = _member(make_tenant, make_user)
    client.force_login(user)
    client.post(_u(tenant, "bills/new/"), _create_data())
    with schema_context(tenant.schema_name):
        bill = Bill.objects.get()
    assert client.post(_u(tenant, f"bills/{bill.pk}/void/")).status_code == 302
    with schema_context(tenant.schema_name):
        bill.refresh_from_db()
        assert bill.status == Bill.Status.VOID
        assert account_balance("accounts_payable") == D("0")


def test_locked_bill_is_read_only(make_tenant, make_user, client):
    tenant, user = _member(make_tenant, make_user)
    client.force_login(user)
    with schema_context(tenant.schema_name):
        org = Organization.objects.create(name="Utility Co")
        bill = Bill.objects.create(
            vendor_organization=org, bill_date=datetime.date(2026, 2, 1), is_locked=True
        )
        from apps.payables.models import BillLine
        BillLine.objects.create(bill=bill, line_type="expense", description="Power",
                                quantity=D("1"), unit_price=D("40"))
        post_bill(bill)
    assert client.get(_u(tenant, f"bills/{bill.pk}/edit/")).status_code == 403
    assert client.get(_u(tenant, f"bills/{bill.pk}/")).status_code == 200


def test_bill_detail_renders(make_tenant, make_user, client):
    tenant, user = _member(make_tenant, make_user)
    client.force_login(user)
    client.post(_u(tenant, "bills/new/"), _create_data())
    with schema_context(tenant.schema_name):
        bill = Bill.objects.get()
    r = client.get(_u(tenant, f"bills/{bill.pk}/"))
    assert r.status_code == 200 and bill.bill_number.encode() in r.content
