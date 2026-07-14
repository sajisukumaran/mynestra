"""Payables bill posting: accrual double-entry across line types, capitalized assets, tax/discount
folding, in-place repost (no reversal), unpost, and vendor balance."""

import datetime
from decimal import Decimal

from django_tenants.utils import schema_context

from apps.finance.models import Account, JournalEntry
from apps.finance.services import account_balance, net_worth
from apps.organizations.models import Organization
from apps.payables.models import AssetItem, Bill, BillLine, VendorProfile
from apps.payables.services import post_bill, repost_bill, unpost_bill, vendor_balance

D = Decimal
JAN = datetime.date(2026, 1, 15)


def _mixed_bill(org):
    """Expense 100 + shipping 10 + tax 8 − discount 5 = 113."""
    bill = Bill.objects.create(vendor_organization=org, bill_date=JAN)
    BillLine.objects.create(bill=bill, line_type="expense", description="Widgets",
                            quantity=D("2"), unit_price=D("50"),
                            account=Account.objects.get(code="5900"))
    BillLine.objects.create(bill=bill, line_type="shipping", description="Shipping",
                            quantity=D("1"), unit_price=D("10"))
    BillLine.objects.create(bill=bill, line_type="tax", description="Sales tax",
                            quantity=D("1"), unit_price=D("8"))
    BillLine.objects.create(bill=bill, line_type="discount", description="Coupon",
                            quantity=D("1"), unit_price=D("5"))
    return bill


def test_accrual_posting_across_line_types(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        org = Organization.objects.create(name="Acme")
        bill = _mixed_bill(org)
        assert bill.total == D("113")
        post_bill(bill)

        assert JournalEntry.objects.count() == 1
        assert account_balance("accounts_payable") == D("113")   # CR liability
        assert account_balance("5900") == D("100")               # DR expense
        assert account_balance("shipping_expense") == D("10")
        assert account_balance("sales_tax_paid") == D("8")
        assert account_balance("purchase_discounts") == D("5")   # CR (reduces net cost)
        bill.refresh_from_db()
        assert bill.status == Bill.Status.OPEN
        assert bill.balance_due == D("113")
        assert bill.journal_entry.source == bill


def test_capitalized_line_posts_to_asset_and_folds_tax(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        org = Organization.objects.create(name="Best Buy")
        before = net_worth()
        bill = Bill.objects.create(vendor_organization=org, bill_date=JAN)
        BillLine.objects.create(
            bill=bill, line_type="item", description="Laptop", quantity=D("1"),
            unit_price=D("1000"), line_tax=D("80"), capitalize=True,
            asset_serial="SN123", warranty_end=datetime.date(2028, 1, 1),
        )
        post_bill(bill)

        assert account_balance("household_goods") == D("1080")   # tax folded into asset cost
        assert account_balance("accounts_payable") == D("1080")
        asset = AssetItem.objects.get(bill_line__bill=bill)
        assert asset.cost == D("1080") and asset.serial_number == "SN123"
        assert asset.warranty_end == datetime.date(2028, 1, 1)
        assert asset.gl_account.system_key == "household_goods"
        # Capitalizing leaves net worth unchanged (asset up = liability up).
        assert net_worth() == before


def test_repost_edits_in_place_without_new_entry(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        org = Organization.objects.create(name="Acme")
        bill = _mixed_bill(org)
        post_bill(bill)
        entry_pk = bill.journal_entry_id
        je_count = JournalEntry.objects.count()

        line = bill.lines.get(line_type="expense")
        line.unit_price = D("60")  # amount 120; total 133
        line.save()
        repost_bill(bill)

        assert JournalEntry.objects.count() == je_count      # no reversal, no new entry
        bill.refresh_from_db()
        assert bill.journal_entry_id == entry_pk
        assert account_balance("accounts_payable") == D("133")
        assert account_balance("5900") == D("120")


def test_uncapitalize_on_repost_removes_asset(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        org = Organization.objects.create(name="Best Buy")
        bill = Bill.objects.create(vendor_organization=org, bill_date=JAN)
        line = BillLine.objects.create(
            bill=bill, line_type="item", description="Laptop", quantity=D("1"),
            unit_price=D("1000"), capitalize=True,
        )
        post_bill(bill)
        assert AssetItem.objects.filter(bill_line__bill=bill).exists()

        line.capitalize = False
        line.save()
        repost_bill(bill)
        assert not AssetItem.objects.filter(bill_line__bill=bill).exists()
        assert account_balance("household_goods") == D("0")
        assert account_balance("5900") == D("1000")  # now expensed


def test_unpost_reverses(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        org = Organization.objects.create(name="Acme")
        bill = _mixed_bill(org)
        post_bill(bill)
        unpost_bill(bill)
        assert account_balance("accounts_payable") == D("0")
        assert account_balance("5900") == D("0")


def test_vendor_balance_sums_open_bills(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        org = Organization.objects.create(name="Acme")
        vp = VendorProfile.objects.create(organization=org)
        post_bill(_mixed_bill(org))
        post_bill(_mixed_bill(org))
        assert vendor_balance(vp) == D("226")  # 113 + 113


def test_bills_with_totals_matches_properties_and_stays_flat(make_tenant):
    """`bills_with_totals` annotates the SAME figures the per-bill properties compute — one query
    however many bills — and dashboard_stats/aging read through it without a per-bill N+1."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from apps.payables.services import aging, bills_with_totals, dashboard_stats

    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        org = Organization.objects.create(name="Acme")
        for _ in range(4):
            post_bill(_mixed_bill(org))

        plain = {b.pk: (b.total, b.amount_paid, b.balance_due) for b in Bill.objects.all()}
        with CaptureQueriesContext(connection) as ctx:
            annotated = {
                b.pk: (b.total, b.amount_paid, b.balance_due)
                for b in bills_with_totals(Bill.objects.all())
            }
        assert annotated == plain
        assert len(ctx.captured_queries) == 1  # subquery aggregates ride the page query

        with CaptureQueriesContext(connection) as dctx:
            stats = dashboard_stats()
            buckets = aging()
        assert stats["total_payable"] == D("452")            # 4 × 113
        assert sum(buckets.values(), D("0")) == D("452")
        assert len(dctx.captured_queries) <= 4               # rollup queries, not one per bill
