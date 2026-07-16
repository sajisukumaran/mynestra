"""Computed balances + derived close: normal-balance signs, rollups, trial balance, net worth,
retained earnings, party & native balances."""

import datetime
from decimal import Decimal

import pytest
from django_tenants.utils import schema_context

from apps.finance.models import Account, AccountType, JournalEntry, Side
from apps.finance.services import (
    LineInput,
    account_balance,
    account_native_balance,
    account_raw_balance,
    current_year_earnings,
    net_income,
    net_worth,
    party_balance,
    post_entry,
    retained_earnings,
    trial_balance,
    void_entry,
)

D = Decimal
JAN = datetime.date(2026, 1, 15)

# (target account, side that increases it, balancing account, balancing side)
NORMAL_CASES = [
    ("1110", "debit", "3100", "credit"),   # Asset — increases on debit
    ("2300", "credit", "1110", "debit"),   # Liability — increases on credit (2100 is now a header)
    ("3100", "credit", "1110", "debit"),   # Equity — increases on credit
    ("4100", "credit", "1110", "debit"),   # Revenue — increases on credit
    ("5200", "debit", "1110", "credit"),   # Expense — increases on debit
]


@pytest.mark.parametrize("target,target_side,other,other_side", NORMAL_CASES)
def test_normal_balance_sign_is_positive(make_tenant, target, target_side, other, other_side):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        post_entry(
            date=JAN,
            lines=[
                LineInput(target, **{target_side: D("100")}),
                LineInput(other, **{other_side: D("100")}),
            ],
        )
        assert account_balance(target) == D("100")


def test_contra_account_reports_flipped_sign(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        # A contra-equity account (e.g. owner's drawings): EQUITY type but DEBIT normal side.
        drawings = Account.objects.create(
            code="3300",
            name="Owner Drawings",
            type=AccountType.EQUITY,
            normal_side=Side.DEBIT,
            parent=Account.objects.get(code="3000"),
        )
        post_entry(
            date=JAN,
            lines=[LineInput("3300", debit=D("40")), LineInput("1110", credit=D("40"))],
        )
        # Debit-normal → a debit balance reads positive (opposite of a normal equity account).
        assert account_balance(drawings) == D("40")
        assert account_raw_balance(drawings) == D("40")


def test_header_account_rolls_up_children(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        post_entry(
            date=JAN,
            lines=[LineInput("1110", debit=D("100")), LineInput("3100", credit=D("100"))],
        )
        post_entry(
            date=JAN,
            lines=[LineInput("1150", debit=D("50")), LineInput("3100", credit=D("50"))],
        )
        assert account_balance("1100") == D("150")  # Cash & Bank header
        assert account_balance("1000") == D("150")  # Assets header


def test_balance_excludes_draft_and_void(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        post_entry(
            date=JAN,
            lines=[LineInput("1110", debit=D("100")), LineInput("3100", credit=D("100"))],
        )
        post_entry(
            date=JAN,
            status=JournalEntry.Status.DRAFT,
            lines=[LineInput("1110", debit=D("999")), LineInput("3100", credit=D("999"))],
        )
        voided = post_entry(
            date=JAN,
            status=JournalEntry.Status.DRAFT,
            lines=[LineInput("1110", debit=D("5")), LineInput("3100", credit=D("5"))],
        )
        void_entry(voided)
        assert account_balance("1110") == D("100")


def test_trial_balance_totals_equal(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        post_entry(
            date=JAN,
            lines=[LineInput("1110", debit=D("100")), LineInput("4100", credit=D("100"))],
        )
        post_entry(
            date=JAN,
            lines=[LineInput("5200", debit=D("30")), LineInput("1110", credit=D("30"))],
        )
        rows = trial_balance()
        assert sum(r.debit_total for r in rows) == sum(r.credit_total for r in rows)
        assert {r.account.code for r in rows} == {"1110", "4100", "5200"}


def test_derived_close_and_net_worth(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        post_entry(
            date=datetime.date(2026, 1, 1),
            lines=[
                LineInput("1110", debit=D("1000")),
                LineInput("opening_balance_equity", credit=D("1000")),
            ],
        )
        post_entry(  # salary
            date=datetime.date(2026, 2, 1),
            lines=[LineInput("1110", debit=D("500")), LineInput("4100", credit=D("500"))],
        )
        post_entry(  # groceries
            date=datetime.date(2026, 3, 1),
            lines=[LineInput("5200", debit=D("200")), LineInput("1110", credit=D("200"))],
        )
        as_of = datetime.date(2026, 12, 31)
        assert net_income(start=datetime.date(2026, 1, 1), end=as_of) == D("300")  # 500 − 200
        assert current_year_earnings(as_of=as_of) == D("300")
        assert net_worth(as_of=as_of) == D("1300")  # cash 1300, no liabilities
        assert retained_earnings(as_of=as_of) == D("0")  # no prior-year activity


def test_net_worth_excludes_contingent_liabilities(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        # A normal loan node (under 2210 Mortgage) and a contingent node (under 2950).
        normal = Account.objects.create(
            code="2210.01",
            name="Mortgage — Acme",
            type=AccountType.LIABILITY,
            normal_side=Side.CREDIT,
            parent=Account.objects.get(code="2210"),
        )
        contingent = Account.objects.create(
            code="2950.01",
            name="Co-signed auto — Son",
            type=AccountType.LIABILITY,
            normal_side=Side.CREDIT,
            parent=Account.objects.get(code="2950"),
        )
        post_entry(
            date=JAN,
            lines=[
                LineInput("1110", debit=D("1000")),
                LineInput("opening_balance_equity", credit=D("1000")),
            ],
        )
        post_entry(  # a mortgage that DOES count toward net worth
            date=JAN,
            lines=[
                LineInput("opening_balance_equity", debit=D("300")),
                LineInput(normal, credit=D("300")),
            ],
        )
        post_entry(  # a contingent co-signed debt that should NOT count
            date=JAN,
            lines=[
                LineInput("opening_balance_equity", debit=D("500")),
                LineInput(contingent, credit=D("500")),
            ],
        )
        # Default: contingent 500 is off-balance-sheet — only the 300 mortgage reduces net worth.
        assert net_worth() == D("700")  # 1000 cash − 300
        # Total-obligations view includes it.
        assert net_worth(include_contingent=True) == D("200")  # 1000 − 300 − 500
        # Both still roll into the liability subtree totals.
        assert account_balance("2000") == D("800")
        assert account_balance("2950") == D("500")


def test_vehicle_asset_node_rolls_up_and_counts_in_net_worth(make_tenant):
    """A per-vehicle 1420.NN node (held at cost) rolls up 1420 → 1400 → Assets and lifts net worth,
    with no special treatment (unlike the 2950 contingent-liability header)."""
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        car = Account.objects.create(
            code="1420.01",
            name="Vehicle — Family SUV",
            type=AccountType.ASSET,
            normal_side=Side.DEBIT,
            parent=Account.objects.get(code="1420"),
        )
        post_entry(  # capitalize a car purchased for cash
            date=JAN,
            lines=[LineInput(car, debit=D("30000")), LineInput("1110", credit=D("30000"))],
        )
        assert account_balance(car) == D("30000")
        assert account_balance("1420") == D("30000")  # header rolls up its child
        assert account_balance("1400") == D("30000")  # Property & Vehicles
        # Net worth is unchanged by moving cash into a like-valued asset (both are assets).
        assert net_worth() == D("0")  # −30000 cash + 30000 vehicle

    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        post_entry(
            date=datetime.date(2025, 6, 1),
            lines=[LineInput("1110", debit=D("400")), LineInput("4100", credit=D("400"))],
        )
        assert retained_earnings(as_of=datetime.date(2026, 3, 1)) == D("400")
        assert current_year_earnings(as_of=datetime.date(2026, 3, 1)) == D("0")


def test_party_balance_aggregates_counterparty(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        from apps.contacts.models import Person

        person = Person.objects.create(first_name="Dr", last_name="Smith")
        post_entry(
            date=JAN,
            lines=[
                LineInput("5410", debit=D("60"), person=person),
                LineInput("1110", credit=D("60")),
            ],
        )
        post_entry(
            date=JAN,
            lines=[
                LineInput("5410", debit=D("40"), person=person),
                LineInput("1110", credit=D("40")),
            ],
        )
        assert party_balance(person=person) == D("100")


def test_native_balance_for_currency_tagged_account(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        from apps.finance.models import Currency

        eur = Currency.objects.get(code="EUR")
        eur_savings = Account.objects.create(
            code="1160",  # a free postable code (1140 is now the seeded CD header)
            name="EUR Savings",
            type=AccountType.ASSET,
            normal_side=Side.DEBIT,
            currency=eur,
            parent=Account.objects.get(code="1100"),
        )
        post_entry(
            date=JAN,
            lines=[
                LineInput("1160", debit=D("100"), currency="EUR", fx_rate=D("1.1")),
                LineInput("3100", credit=D("110")),
            ],
        )
        assert account_native_balance(eur_savings) == D("100")  # own currency
        assert account_balance(eur_savings) == D("110")  # base
        assert account_native_balance(Account.objects.get(code="1110")) is None  # untagged
