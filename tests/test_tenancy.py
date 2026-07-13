"""Tenant-isolation gate (standing). Now with real model data, not just raw schemas."""

import datetime
from decimal import Decimal

from django.db import connection
from django_tenants.utils import schema_context

from apps.contacts.models import Person
from apps.families.models import Family
from apps.finance.models import Account, JournalEntry, JournalLine
from apps.finance.services import LineInput, post_entry
from apps.organizations.models import Organization
from apps.relationships.models import (
    PersonOrgRelationship,
    PersonOrgRelationshipType,
    PersonRelationship,
    RelationshipType,
)
from apps.setup.models import Category


def _schema_exists(name: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", [name]
        )
        return cursor.fetchone() is not None


def test_two_tenants_get_isolated_schemas_and_data(make_tenant):
    a = make_tenant(name="Alpha")
    b = make_tenant(name="Beta")

    assert _schema_exists(a.schema_name)
    assert _schema_exists(b.schema_name)
    assert a.schema_name != b.schema_name

    # A row written in tenant A's schema must be invisible from tenant B (zero cross-schema leak).
    with schema_context(a.schema_name):
        baseline = Category.objects.count()
        Category.objects.create(kind=Category.Kind.ORG, name="Alpha-Only Bank", color="blue")
        assert Category.objects.count() == baseline + 1

    with schema_context(b.schema_name):
        assert not Category.objects.filter(name="Alpha-Only Bank").exists()
        assert Category.objects.count() == baseline  # same seeded baseline, no leak


def test_families_and_relationships_are_isolated(make_tenant):
    """P5 models (Family, PersonRelationship) must not leak across tenant schemas."""
    a = make_tenant(name="Alpha")
    b = make_tenant(name="Beta")

    with schema_context(a.schema_name):
        Family.objects.create(name="Alpha-Only Family")
        pa = Person.objects.create(first_name="Alpha", last_name="A", gender="M")
        pb = Person.objects.create(first_name="Alpha", last_name="B", gender="F")
        PersonRelationship.objects.create(
            person_a=pa, person_b=pb, type=RelationshipType.objects.get(code="spouse")
        )
        assert Family.objects.count() == 1
        assert PersonRelationship.objects.count() == 1

    with schema_context(b.schema_name):
        assert Family.objects.count() == 0
        assert PersonRelationship.objects.count() == 0
        assert not Family.objects.filter(name="Alpha-Only Family").exists()


def test_organizations_and_p2o_are_isolated(make_tenant):
    """P6 models (Organization, PersonOrgRelationship) must not leak across tenant schemas."""
    a = make_tenant(name="Alpha")
    b = make_tenant(name="Beta")

    with schema_context(a.schema_name):
        org = Organization.objects.create(name="Alpha-Only Bank")
        person = Person.objects.create(first_name="Alpha", last_name="Owner")
        PersonOrgRelationship.objects.create(
            person=person, organization=org,
            type=PersonOrgRelationshipType.objects.get(code="account_holder"),
        )
        assert Organization.objects.count() == 1
        assert PersonOrgRelationship.objects.count() == 1

    with schema_context(b.schema_name):
        assert Organization.objects.count() == 0
        assert PersonOrgRelationship.objects.count() == 0
        assert not Organization.objects.filter(name="Alpha-Only Bank").exists()


def test_finance_ledger_source_and_party_are_isolated(make_tenant):
    """Finance models (Account/JournalEntry/JournalLine), the source GenericFK, and the line
    counterparty FKs must not leak across tenant schemas."""
    a = make_tenant(name="Alpha")
    b = make_tenant(name="Beta")

    with schema_context(a.schema_name):
        seeded = Account.objects.count()
        assert seeded > 0  # COA backfilled into every schema
        org = Organization.objects.create(name="Alpha-Only Bank")
        person = Person.objects.create(first_name="Alpha", last_name="Payer")
        entry = post_entry(
            date=datetime.date(2026, 1, 5),
            source=org,
            lines=[
                LineInput("5400", debit=Decimal("100"), person=person),
                LineInput("1110", credit=Decimal("100")),
            ],
        )
        assert entry.source == org  # source GenericFK resolves within this tenant
        assert JournalEntry.objects.count() == 1
        assert JournalLine.objects.filter(person=person).count() == 1

    with schema_context(b.schema_name):
        assert JournalEntry.objects.count() == 0
        assert JournalLine.objects.count() == 0
        assert Account.objects.count() == seeded  # same seeded COA, no cross-schema leak
        assert not Organization.objects.filter(name="Alpha-Only Bank").exists()


def test_banking_accounts_transactions_and_ledger_are_isolated(make_tenant):
    """Module 3 (BankAccount/Holder/BankTransaction) and the GL entries they post must not leak
    across tenant schemas."""
    from apps.banking.models import BankAccount, BankAccountHolder, BankTransaction, TxnType
    from apps.banking.services import post_transaction

    a = make_tenant(name="Alpha")
    b = make_tenant(name="Beta")

    with schema_context(a.schema_name):
        bank = Organization.objects.create(name="Alpha-Only Bank")
        person = Person.objects.create(first_name="Alpha", last_name="Holder")
        account = BankAccount.objects.create(
            bank=bank, account_type="checking", nickname="Alpha Checking", currency_id="USD",
        )
        BankAccountHolder.objects.create(account=account, person=person, is_primary=True)
        txn = BankTransaction.objects.create(
            account=account, txn_type=TxnType.OPENING, date=datetime.date(2026, 1, 5),
            amount=Decimal("500"),
        )
        post_transaction(txn)
        assert BankAccount.objects.count() == 1
        assert BankTransaction.objects.count() == 1
        assert account.balance == Decimal("500")

    with schema_context(b.schema_name):
        assert BankAccount.objects.count() == 0
        assert BankAccountHolder.objects.count() == 0
        assert BankTransaction.objects.count() == 0
        assert not BankAccount.objects.filter(nickname="Alpha Checking").exists()
        # The opening entry posted in Alpha must not appear in Beta's ledger.
        assert JournalEntry.objects.count() == 0


def test_investments_accounts_securities_lots_and_ledger_are_isolated(make_tenant):
    """Module 5 (InvestmentAccount/Security/Lot/InvestmentTransaction) and the GL entries they post
    must not leak across tenant schemas."""
    from apps.investments.models import (
        InvestmentAccount,
        InvestmentTransaction,
        InvTxnType,
        Lot,
        Security,
        VestingGrant,
        VestingTranche,
    )
    from apps.investments.services import apply_transaction, ensure_gl_account

    a = make_tenant(name="Alpha")
    b = make_tenant(name="Beta")

    with schema_context(a.schema_name):
        acct = InvestmentAccount.objects.create(
            institution=Organization.objects.create(name="Alpha-Only Broker"),
            nickname="Alpha Taxable", registration="taxable_individual", currency_id="USD",
        )
        ensure_gl_account(acct)
        sec = Security.objects.create(symbol="ALPH", name="Alpha Co", currency_id="USD")
        opening = InvestmentTransaction.objects.create(
            account=acct, txn_type=InvTxnType.OPENING,
            date=datetime.date(2026, 1, 5), amount=Decimal("1000"))
        apply_transaction(opening, is_new=True)
        buy = InvestmentTransaction.objects.create(
            account=acct, txn_type=InvTxnType.BUY, date=datetime.date(2026, 1, 6),
            security=sec, quantity=Decimal("10"), price=Decimal("50"), amount=Decimal("500"))
        apply_transaction(buy, is_new=True)
        grant = VestingGrant.objects.create(
            account=acct, kind="dollar", label="Alpha Match",
            grant_date=datetime.date(2026, 1, 1), total=Decimal("1000"), funded=True)
        VestingTranche.objects.create(
            grant=grant, vest_date=datetime.date(2027, 1, 1), cumulative_percent=Decimal("100"))
        assert InvestmentAccount.objects.count() == 1
        assert Lot.objects.count() == 1
        assert VestingGrant.objects.count() == 1
        assert acct.balance == Decimal("1000")  # cash 500 + cost 500

    with schema_context(b.schema_name):
        assert InvestmentAccount.objects.count() == 0
        assert Security.objects.count() == 0
        assert Lot.objects.count() == 0
        assert InvestmentTransaction.objects.count() == 0
        assert VestingGrant.objects.count() == 0
        assert VestingTranche.objects.count() == 0
        assert not InvestmentAccount.objects.filter(nickname="Alpha Taxable").exists()
        assert JournalEntry.objects.count() == 0


def test_payables_bills_payments_and_catalog_are_isolated(make_tenant):
    """Module 6 (VendorProfile/Item/Bill/BillLine/Payment) and the AP entries they post must not
    leak across tenant schemas."""
    from apps.payables.models import Bill, BillLine, Item, Payment, VendorProfile
    from apps.payables.services import apply_payment, post_bill

    a = make_tenant(name="Alpha")
    b = make_tenant(name="Beta")

    with schema_context(a.schema_name):
        org = Organization.objects.create(name="Alpha-Only Vendor")
        VendorProfile.objects.create(organization=org)
        Item.objects.create(name="Alpha Widget", kind="good", upc="ALPHA1")
        bill = Bill.objects.create(vendor_organization=org, bill_date=datetime.date(2026, 1, 5))
        BillLine.objects.create(bill=bill, line_type="expense", description="Widgets",
                                quantity=Decimal("1"), unit_price=Decimal("100"))
        post_bill(bill)
        payment = Payment.objects.create(
            vendor_organization=org, date=datetime.date(2026, 1, 6),
            amount=Decimal("100"), funding_kind="cash",
        )
        apply_payment(payment, [(bill, Decimal("100"))])
        bill.refresh_from_db()
        assert Bill.objects.count() == 1 and Payment.objects.count() == 1
        assert VendorProfile.objects.count() == 1 and Item.objects.count() == 1
        assert bill.status == Bill.Status.PAID

    with schema_context(b.schema_name):
        assert Bill.objects.count() == 0
        assert Payment.objects.count() == 0
        assert VendorProfile.objects.count() == 0
        assert Item.objects.count() == 0
        assert not Organization.objects.filter(name="Alpha-Only Vendor").exists()
        assert JournalEntry.objects.count() == 0
