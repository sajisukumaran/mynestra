"""Loans under Expert mode: a per-loan posting map remaps the interest (escrow / fee) leg, while
Standard-mode postings use the defaults even if a mapping exists."""

import datetime
from decimal import Decimal

from django.db import connection
from django_tenants.utils import schema_context

from apps.finance.models import Account, Currency, Side
from apps.finance.models import AccountType as GLType
from apps.finance.services import account_balance, set_posting_map
from apps.loans.models import Funding, Loan, LoanTransaction, LoanTxnType
from apps.loans.services import post_transaction
from apps.tenants.models import Tenant

D = Decimal
JAN = datetime.date(2026, 1, 15)


def _set_mode(tenant, mode):
    connection.set_schema_to_public()
    Tenant.objects.filter(pk=tenant.pk).update(accounting_mode=mode)


def _loan_with_opening(amount="20000"):
    loan = Loan.objects.create(
        loan_type="auto", nickname="Car", currency=Currency.objects.get(code="USD"),
        principal_original=D(amount),
    )
    opening = LoanTransaction.objects.create(
        loan=loan, txn_type=LoanTxnType.OPENING, date=JAN, amount=D(amount)
    )
    post_transaction(opening)
    return loan


def _custom_expense(code="5901", name="Auto loan interest"):
    return Account.objects.create(
        code=code, name=name, type=GLType.EXPENSE, normal_side=Side.DEBIT,
        parent=Account.objects.get(code="5000"), is_postable=True, is_system=False,
    )


def _cash_payment(loan, *, interest="100"):
    pay = LoanTransaction.objects.create(
        loan=loan, txn_type=LoanTxnType.PAYMENT, date=JAN, amount=D("400") + D(interest),
        principal=D("400"), interest=D(interest), funding_source=Funding.CASH,
    )
    post_transaction(pay)
    return pay


def test_expert_remaps_interest_account(make_tenant):
    tenant = make_tenant()
    _set_mode(tenant, "expert")
    with schema_context(tenant.schema_name):
        loan = _loan_with_opening()
        custom = _custom_expense()
        set_posting_map(loan, "interest_expense", custom)
        _cash_payment(loan)
        assert account_balance(custom) == D("100")     # interest went to the mapped account
        assert account_balance("5860") == D("0")        # not the default


def test_standard_mode_ignores_mapping(make_tenant):
    tenant = make_tenant()  # standard by default
    with schema_context(tenant.schema_name):
        loan = _loan_with_opening()
        custom = _custom_expense()
        set_posting_map(loan, "interest_expense", custom)  # saved, but ignored in Standard
        _cash_payment(loan)
        assert account_balance("5860") == D("100")     # default interest expense used
        assert account_balance(custom) == D("0")
