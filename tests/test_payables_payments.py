"""Payables payments: cash / bank funding, allocation across a vendor's bills, status transitions,
delete (unapply reverses + reopens), dashboard, and the live launcher tile."""

import datetime
from decimal import Decimal

from django_tenants.utils import schema_context

from apps.banking.models import AccountType, BankAccount, BankTransaction
from apps.banking.services import ensure_gl_account as bank_gl
from apps.finance.models import Currency
from apps.finance.services import account_balance
from apps.organizations.models import Organization
from apps.payables.models import Bill, BillLine, Payment
from apps.payables.services import post_bill
from apps.tenants.models import Membership, Role

D = Decimal
FEB = datetime.date(2026, 2, 1)


def _member(make_tenant, make_user, name="Acme HH", email="m@example.com"):
    tenant = make_tenant(name=name)
    user = make_user(email)
    Membership.objects.create(user=user, tenant=tenant, role=Role.MEMBER)
    return tenant, user


def _u(tenant, path=""):
    return f"/t/{tenant.schema_name}/payables/{path}"


def _bill(org, amount="100"):
    bill = Bill.objects.create(vendor_organization=org, bill_date=FEB)
    BillLine.objects.create(bill=bill, line_type="expense", description="Stuff",
                            quantity=D("1"), unit_price=D(amount))
    post_bill(bill)
    return bill


def test_cash_payment_full_settles_bill(make_tenant, make_user, client):
    tenant, user = _member(make_tenant, make_user)
    client.force_login(user)
    with schema_context(tenant.schema_name):
        org = Organization.objects.create(name="Acme")
        bill = _bill(org, "100")
    resp = client.post(_u(tenant, "payments/new/"), {
        "vendor_kind": "organization", "vendor_id": str(org.pk),
        "date": "2026-02-05", "funding_kind": "cash", "cash_account": "",
        f"alloc_{bill.pk}": "100", "reference": "", "notes": "",
    })
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        bill.refresh_from_db()
        assert bill.status == Bill.Status.PAID and bill.balance_due == D("0")
        assert account_balance("accounts_payable") == D("0")
        assert Payment.objects.count() == 1


def test_cash_payment_partial(make_tenant, make_user, client):
    tenant, user = _member(make_tenant, make_user)
    client.force_login(user)
    with schema_context(tenant.schema_name):
        org = Organization.objects.create(name="Acme")
        bill = _bill(org, "100")
    client.post(_u(tenant, "payments/new/"), {
        "vendor_kind": "organization", "vendor_id": str(org.pk),
        "date": "2026-02-05", "funding_kind": "cash", "cash_account": "",
        f"alloc_{bill.pk}": "40",
    })
    with schema_context(tenant.schema_name):
        bill.refresh_from_db()
        assert bill.status == Bill.Status.PARTIALLY_PAID and bill.balance_due == D("60")
        assert account_balance("accounts_payable") == D("60")


def test_one_payment_settles_multiple_bills(make_tenant, make_user, client):
    tenant, user = _member(make_tenant, make_user)
    client.force_login(user)
    with schema_context(tenant.schema_name):
        org = Organization.objects.create(name="Acme")
        b1 = _bill(org, "100")
        b2 = _bill(org, "50")
    client.post(_u(tenant, "payments/new/"), {
        "vendor_kind": "organization", "vendor_id": str(org.pk),
        "date": "2026-02-05", "funding_kind": "cash", "cash_account": "",
        f"alloc_{b1.pk}": "100", f"alloc_{b2.pk}": "50",
    })
    with schema_context(tenant.schema_name):
        b1.refresh_from_db()
        b2.refresh_from_db()
        assert b1.status == Bill.Status.PAID and b2.status == Bill.Status.PAID
        assert account_balance("accounts_payable") == D("0")
        assert Payment.objects.get().amount == D("150")


def test_bank_payment_creates_withdrawal(make_tenant, make_user, client):
    tenant, user = _member(make_tenant, make_user)
    client.force_login(user)
    with schema_context(tenant.schema_name):
        org = Organization.objects.create(name="Acme")
        bill = _bill(org, "100")
        acct = BankAccount.objects.create(
            bank=Organization.objects.create(name="HDFC"), account_type=AccountType.CHECKING,
            nickname="Checking", number="1", currency=Currency.objects.get(code="USD"),
        )
        bank_gl(acct)
        gl_before = account_balance(acct.gl_account)
    resp = client.post(_u(tenant, "payments/new/"), {
        "vendor_kind": "organization", "vendor_id": str(org.pk),
        "date": "2026-02-05", "funding_kind": "bank", "bank_account": str(acct.pk),
        f"alloc_{bill.pk}": "100",
    })
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        bill.refresh_from_db()
        assert bill.status == Bill.Status.PAID
        assert account_balance("accounts_payable") == D("0")
        assert BankTransaction.objects.filter(
            account=acct, txn_type="withdrawal", amount=D("100")
        ).exists()
        assert account_balance(acct.gl_account) == gl_before - D("100")


def test_delete_payment_reopens_bill(make_tenant, make_user, client):
    tenant, user = _member(make_tenant, make_user)
    client.force_login(user)
    with schema_context(tenant.schema_name):
        org = Organization.objects.create(name="Acme")
        bill = _bill(org, "100")
    client.post(_u(tenant, "payments/new/"), {
        "vendor_kind": "organization", "vendor_id": str(org.pk),
        "date": "2026-02-05", "funding_kind": "cash", "cash_account": "",
        f"alloc_{bill.pk}": "100",
    })
    with schema_context(tenant.schema_name):
        payment = Payment.objects.get()
    assert client.post(_u(tenant, f"payments/{payment.pk}/delete/")).status_code == 302
    with schema_context(tenant.schema_name):
        bill.refresh_from_db()
        assert bill.status == Bill.Status.OPEN and bill.balance_due == D("100")
        assert account_balance("accounts_payable") == D("100")
        # Delete erases the record entirely (not a soft-delete) and leaves no reversal behind.
        assert not Payment.all_objects.filter(pk=payment.pk).exists()


def test_edit_payment_reallocates(make_tenant, make_user, client):
    """Editing a payment re-posts it in place: old funding torn down, new allocations applied,
    bills recomputed, and it stays one payment (not a duplicate)."""
    tenant, user = _member(make_tenant, make_user)
    client.force_login(user)
    with schema_context(tenant.schema_name):
        org = Organization.objects.create(name="Acme")
        b1 = _bill(org, "100")
        b2 = _bill(org, "50")
    client.post(_u(tenant, "payments/new/"), {
        "vendor_kind": "organization", "vendor_id": str(org.pk),
        "date": "2026-02-05", "funding_kind": "cash", "cash_account": "",
        f"alloc_{b1.pk}": "100",
    })
    with schema_context(tenant.schema_name):
        payment = Payment.objects.get()
    assert client.get(_u(tenant, f"payments/{payment.pk}/edit/")).status_code == 200
    resp = client.post(_u(tenant, f"payments/{payment.pk}/edit/"), {
        "vendor_kind": "organization", "vendor_id": str(org.pk),
        "date": "2026-02-06", "funding_kind": "cash", "cash_account": "",
        f"alloc_{b1.pk}": "40", f"alloc_{b2.pk}": "50",
    })
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        assert Payment.all_objects.count() == 1        # edited in place, not duplicated
        payment.refresh_from_db()
        assert payment.amount == D("90")
        assert payment.date == datetime.date(2026, 2, 6)
        b1.refresh_from_db()
        b2.refresh_from_db()
        assert b1.status == Bill.Status.PARTIALLY_PAID and b1.balance_due == D("60")
        assert b2.status == Bill.Status.PAID
        assert account_balance("accounts_payable") == D("60")


def test_dashboard_and_launcher_tile(make_tenant, make_user, client):
    tenant, user = _member(make_tenant, make_user)
    client.force_login(user)
    with schema_context(tenant.schema_name):
        org = Organization.objects.create(name="Acme")
        _bill(org, "100")
    assert client.get(_u(tenant)).status_code == 200          # dashboard
    assert b"Payables" in client.get(f"/t/{tenant.schema_name}/").content  # live launcher tile
