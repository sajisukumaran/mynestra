"""Banking model invariants: masked number, per-account GL code sequencing, holder uniqueness,
lifecycle dates, and the DB CHECK constraints (one payee, positive amount)."""

import datetime
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django_tenants.utils import schema_context

from apps.banking.models import (
    AccountType,
    BankAccount,
    BankAccountHolder,
    BankTransaction,
    TxnType,
)
from apps.banking.services import ensure_gl_account
from apps.contacts.models import Person
from apps.organizations.models import Organization

D = Decimal
JAN = datetime.date(2026, 1, 15)


def _account(number="1234567890", account_type=AccountType.CHECKING, nickname="A"):
    bank = Organization.objects.create(name="Bank")
    return BankAccount.objects.create(
        bank=bank, account_type=account_type, nickname=nickname, number=number, currency_id="USD"
    )


def test_masked_number(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        assert _account(number="1234567890").masked_number == "••••7890"
        assert _account(number="99").masked_number == "99"
        assert _account(number="").masked_number == ""


def test_gl_codes_sequence_under_type_header(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        a1, a2 = _account(), _account()
        s1 = _account(account_type=AccountType.SAVINGS)
        cd1 = _account(account_type=AccountType.CD)
        assert ensure_gl_account(a1).code == "1120.01"
        assert ensure_gl_account(a2).code == "1120.02"
        assert ensure_gl_account(s1).code == "1130.01"
        assert ensure_gl_account(cd1).code == "1140.01"  # CDs nest under the 1140 header


def test_cd_fields_and_helpers(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        today = datetime.date.today()
        cd = _account(account_type=AccountType.CD)
        cd.apr = D("4.5")
        cd.term_months = 12
        cd.maturity_date = today + datetime.timedelta(days=30)
        cd.save()
        assert cd.is_cd
        assert not cd.is_matured
        assert cd.days_to_maturity == 30
        assert cd.apr_display == "4.5%"
        # A CD past its maturity reads as matured (negative days remaining).
        cd.maturity_date = today - datetime.timedelta(days=5)
        assert cd.is_matured and cd.days_to_maturity == -5
        # Non-CD accounts report is_cd False and no maturity.
        chk = _account(account_type=AccountType.CHECKING)
        assert not chk.is_cd and chk.days_to_maturity is None


def test_display_balance_zero_before_posting(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        a = _account()
        ensure_gl_account(a)
        assert a.display_balance == D("0")


def test_lifecycle_dates(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        a = _account()
        a.opened_year, a.opened_month = 2020, 6
        a.closed_year, a.closed_month, a.closed_day = 2024, 3, 14
        a.save()
        assert a.opened.is_set and not a.opened.day
        assert a.is_closed and a.closed.display == "14-Mar-2024"


def test_holder_is_unique_per_account(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        a = _account()
        p = Person.objects.create(first_name="Asha", last_name="R")
        BankAccountHolder.objects.create(account=a, person=p, is_primary=True)
        with pytest.raises(IntegrityError), transaction.atomic():
            BankAccountHolder.objects.create(account=a, person=p)


def test_check_rejects_two_payees(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        a = _account()
        p = Person.objects.create(first_name="P", last_name="One")
        o = Organization.objects.create(name="Payee Co")
        with pytest.raises(IntegrityError), transaction.atomic():
            BankTransaction.objects.create(
                account=a, txn_type=TxnType.DEPOSIT, date=JAN, amount=D("5"),
                payee_person=p, payee_organization=o,
            )


def test_check_rejects_non_positive_amount(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        a = _account()
        with pytest.raises(IntegrityError), transaction.atomic():
            BankTransaction.objects.create(
                account=a, txn_type=TxnType.DEPOSIT, date=JAN, amount=D("0")
            )
