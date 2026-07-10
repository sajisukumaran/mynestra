"""Cards models: masked numbers, GL code sequencing, utilization math + tints, network labels,
PartialDate lifecycle, holder uniqueness, the CHECK constraints, and debit-card delegation."""

import datetime
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django_tenants.utils import schema_context

from apps.banking.models import BankAccount
from apps.cards.models import (
    CardTxnType,
    CreditCard,
    CreditCardHolder,
    CreditCardTransaction,
    DebitCard,
)
from apps.cards.services import ensure_gl_account
from apps.contacts.models import Person
from apps.finance.models import Currency
from apps.organizations.models import Organization

D = Decimal


def _card(nickname="Amex", number="123456789012", **kw):
    issuer = Organization.objects.create(name="Issuer")
    return CreditCard.objects.create(
        issuer=issuer, nickname=nickname, number=number,
        currency=Currency.objects.get(code="USD"), **kw,
    )


def test_masked_number_and_network_label(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        card = _card(number="123456789012", network="amex")
        assert card.masked_number == "••••9012"
        assert card.network_label == "American Express"
        assert _card(nickname="short", number="12").masked_number == "12"
        assert _card(nickname="none", number="").masked_number == ""


def test_gl_code_sequencing_under_2100(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        a = ensure_gl_account(_card(nickname="A"))
        b = ensure_gl_account(_card(nickname="B"))
        assert a.code == "2100.01" and b.code == "2100.02"
        assert a.parent.code == "2100"


def test_utilization_and_tints(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        card = _card(credit_limit=D("1000"))
        # No postings yet → 0% utilization, no tint.
        assert card.utilization == 0
        assert card.available_credit == D("1000")
        assert card.utilization_tint == ""
        # No limit → utilization/available are None.
        no_limit = _card(nickname="NL")
        assert no_limit.utilization is None and no_limit.available_credit is None


def test_utilization_tint_thresholds(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        card = _card(credit_limit=D("1000"))
        CreditCardTransaction.objects.create(
            card=card, txn_type=CardTxnType.OPENING, date=datetime.date(2026, 1, 1), amount=D("750")
        )
        from apps.cards.services import post_transaction

        post_transaction(card.transactions.first())
        card.refresh_from_db()
        assert card.utilization_tint == "warning"  # 75%


def test_partialdate_lifecycle(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        card = _card(opened_year=2020, opened_month=6, closed_year=2026)
        assert card.opened.is_set and card.is_closed


def test_holder_uniqueness(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        card = _card()
        person = Person.objects.create(first_name="Raj", last_name="S")
        CreditCardHolder.objects.create(card=card, person=person)
        with pytest.raises(IntegrityError), transaction.atomic():
            CreditCardHolder.objects.create(card=card, person=person)


def test_one_payee_check(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        card = _card()
        p = Person.objects.create(first_name="A", last_name="B")
        o = Organization.objects.create(name="Shop")
        with pytest.raises(IntegrityError), transaction.atomic():
            CreditCardTransaction.objects.create(
                card=card, txn_type=CardTxnType.CHARGE, date=datetime.date(2026, 1, 1),
                amount=D("10"), payee_person=p, payee_organization=o,
            )


def test_amount_positive_check(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        card = _card()
        with pytest.raises(IntegrityError), transaction.atomic():
            CreditCardTransaction.objects.create(
                card=card, txn_type=CardTxnType.CHARGE, date=datetime.date(2026, 1, 1),
                amount=D("0"),
            )


def test_debit_card_delegates_balance_and_expiry(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        bank = Organization.objects.create(name="HDFC Bank")
        acct = BankAccount.objects.create(
            bank=bank, account_type="checking", nickname="Chk", currency_id="USD"
        )
        card = DebitCard.objects.create(
            bank_account=acct, nickname="Debit", number="4111111111119999",
            network="visa", expiry_month=8, expiry_year=2029,
        )
        assert card.masked_number == "••••9999"
        assert card.expiry_display == "08/29"
        assert card.network_label == "Visa"
        assert card.display_balance == acct.display_balance
