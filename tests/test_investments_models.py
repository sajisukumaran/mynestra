"""Investment model behaviour: registration → GL group, masked number, latest price / market value /
unrealized gain, per-type cash effect, CD attributes, and the transaction CHECK constraints."""

import datetime
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django_tenants.utils import schema_context

from apps.contacts.models import Person
from apps.finance.models import Currency
from apps.investments.models import (
    AccountGroup,
    InvestmentAccount,
    InvestmentTransaction,
    InvTxnType,
    Security,
    SecurityKind,
    SecurityPrice,
)
from apps.investments.services import ensure_gl_account
from apps.organizations.models import Organization

D = Decimal


def _acct(registration="taxable_individual", number=""):
    return InvestmentAccount.objects.create(
        institution=Organization.objects.create(name="Broker"),
        nickname="Acct", registration=registration, number=number,
        currency=Currency.objects.get(code="USD"),
    )


def test_registration_maps_to_group(make_tenant):
    with schema_context(make_tenant().schema_name):
        assert _acct("taxable_individual").group == AccountGroup.TAXABLE
        assert _acct("401k").group == AccountGroup.RETIREMENT
        assert _acct("roth_ira").group == AccountGroup.RETIREMENT
        assert _acct("hsa").group == AccountGroup.HSA


def test_masked_number(make_tenant):
    with schema_context(make_tenant().schema_name):
        assert _acct(number="1234567890").masked_number == "••••7890"
        assert _acct(number="").masked_number == ""


def test_latest_price_and_market_value(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _acct()
        ensure_gl_account(acct)
        sec = Security.objects.create(symbol="ACME", name="Acme",
                                      currency=Currency.objects.get(code="USD"))
        SecurityPrice.objects.create(security=sec, as_of=datetime.date(2026, 1, 1), price=D("10"))
        SecurityPrice.objects.create(security=sec, as_of=datetime.date(2026, 6, 1), price=D("15"))
        assert sec.latest_price == D("15")  # most recent
        # Buy 10 @ 10 → cost 100; at price 15 → market 150, unrealized 50.
        from apps.investments.services import apply_transaction
        txn = InvestmentTransaction.objects.create(
            account=acct, txn_type=InvTxnType.BUY, date=datetime.date(2026, 2, 1),
            security=sec, quantity=D("10"), price=D("10"), amount=D("100"))
        apply_transaction(txn, is_new=True)
        assert acct.market_value == D("150")
        assert acct.unrealized_gain == D("50")
        assert acct.total_value == acct.cash_balance + D("150")


def test_market_value_falls_back_to_cost_without_price(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _acct()
        ensure_gl_account(acct)
        sec = Security.objects.create(symbol="NOPX", name="No Price",
                                      currency=Currency.objects.get(code="USD"))
        from apps.investments.services import apply_transaction
        txn = InvestmentTransaction.objects.create(
            account=acct, txn_type=InvTxnType.BUY, date=datetime.date(2026, 2, 1),
            security=sec, quantity=D("4"), price=D("25"), amount=D("100"))
        apply_transaction(txn, is_new=True)
        assert acct.market_value == D("100")     # no price → cost basis
        assert acct.unrealized_gain == D("0")


def test_cd_attributes(make_tenant):
    with schema_context(make_tenant().schema_name):
        cd = Security.objects.create(
            symbol="", name="12-mo CD", kind=SecurityKind.CD,
            currency=Currency.objects.get(code="USD"),
            apr=D("5.25"), maturity_date=datetime.date(2026, 12, 31))
        assert cd.is_cd
        assert cd.display == "12-mo CD"


def test_signed_cash_per_type(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _acct()

        def sc(ttype, **kw):
            return InvestmentTransaction(account=acct, txn_type=ttype, **kw).signed_cash

        assert sc(InvTxnType.CONTRIBUTION, amount=D("100")) == D("100")
        assert sc(InvTxnType.WITHDRAWAL, amount=D("100")) == D("-100")
        assert sc(InvTxnType.BUY, amount=D("100"), fee=D("5")) == D("-105")   # cash out incl fee
        assert sc(InvTxnType.SELL, amount=D("100"), fee=D("5")) == D("95")     # net proceeds
        assert sc(InvTxnType.DIVIDEND_REINVEST, amount=D("30")) == D("0")      # cash-neutral
        assert sc(InvTxnType.SPLIT) == D("0")


def test_amount_nonneg_check(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _acct()
        with pytest.raises(IntegrityError), transaction.atomic():
            InvestmentTransaction.objects.create(
                account=acct, txn_type=InvTxnType.DIVIDEND,
                date=datetime.date(2026, 1, 1), amount=D("-1"))


def test_one_payee_check(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _acct()
        person = Person.objects.create(first_name="Pat", last_name="Payer")
        org = Organization.objects.create(name="Payer Inc")
        with pytest.raises(IntegrityError), transaction.atomic():
            InvestmentTransaction.objects.create(
                account=acct, txn_type=InvTxnType.DIVIDEND, date=datetime.date(2026, 1, 1),
                amount=D("10"), payee_person=person, payee_organization=org)
