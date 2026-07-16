"""Health P3 — prescriptions + the reminders feed. A prescription fill routes through Payables as a
locked bill to Pharmacy / Prescriptions (5440) with partial payments (bank / card / cash / HSA),
exactly like a provider invoice; the HSA path settles AP straight from the health-savings account
(invariant intact); and `reminders_due` merges due invoices, scheduled appointments, refills and
plan-year resets soonest-first."""

import datetime
from decimal import Decimal

from django_tenants.utils import schema_context

from apps.finance.models import JournalEntry
from apps.finance.services import account_balance, net_worth
from apps.health.models import (
    Encounter,
    EncounterType,
    Funding,
    InvoiceStatus,
    Prescription,
    VisitStatus,
)
from apps.health.services import (
    delete_prescription,
    record_prescription_payment,
    reminders_due,
    save_invoice,
    save_prescription,
)

D = Decimal
ZERO = D("0")
JAN = datetime.date(2026, 1, 15)
FEB = datetime.date(2026, 2, 15)


# --- helpers (inside schema_context) ---------------------------------------------------------

def _org(name="Corner Pharmacy"):
    from apps.organizations.models import Organization

    return Organization.objects.create(name=name)


def _usd():
    from apps.finance.models import Currency

    return Currency.objects.get(code="USD")


def _person(first="Sam", last="Rivera"):
    from apps.contacts.models import Person

    return Person.objects.create(first_name=first, last_name=last)


def _bank(nickname="Checking"):
    from apps.banking.models import AccountType as BAT
    from apps.banking.models import BankAccount
    from apps.banking.services import ensure_gl_account as bank_gl

    acct = BankAccount.objects.create(
        bank=_org("My Bank"), account_type=BAT.CHECKING, nickname=nickname, currency=_usd()
    )
    bank_gl(acct)
    return acct


def _hsa(nickname="HSA", opening="5000"):
    from apps.investments.models import InvestmentAccount, InvestmentTransaction, InvTxnType
    from apps.investments.services import apply_transaction, ensure_gl_account

    acct = InvestmentAccount.objects.create(
        institution=_org("HSA Bank"), nickname=nickname, registration="hsa", currency=_usd()
    )
    ensure_gl_account(acct)
    if opening:
        txn = InvestmentTransaction.objects.create(
            account=acct, txn_type=InvTxnType.OPENING, date=JAN, amount=D(opening)
        )
        apply_transaction(txn, is_new=True)
    return acct


def _rx(*, patient=None, pharmacy=None, cost="40", date=JAN, drug="Atorvastatin",
        days_supply=None, refills=0, save=True, user=None):
    rx = Prescription(
        patient=patient or _person(), pharmacy_organization=pharmacy or _org(),
        drug_name=drug, date=date, cost=D(cost), days_supply=days_supply,
        refills_remaining=refills,
    )
    rx.save()
    if save:
        save_prescription(rx, is_new=True, user=user)
    rx.refresh_from_db()
    return rx


def _hsa_inv(acct):
    from apps.investments.services import cash_balance, cost_basis

    acct.refresh_from_db()
    return account_balance(acct.gl_account) == cash_balance(acct) + cost_basis(acct)


def _assert_balanced():
    from django.db.models import Sum

    from apps.finance.models import JournalLine

    agg = JournalLine.objects.filter(entry__status=JournalEntry.Status.POSTED).aggregate(
        d=Sum("base_debit"), c=Sum("base_credit")
    )
    assert agg["d"] == agg["c"], (agg["d"], agg["c"])


# --- the locked bill posts to pharmacy_expense (5440) ----------------------------------------

def test_prescription_bill_posts_to_pharmacy(make_tenant):
    with schema_context(make_tenant().schema_name):
        rx = _rx(cost="60")
        assert rx.bill is not None and rx.bill.is_locked
        assert rx.status == InvoiceStatus.UNPAID
        assert account_balance("pharmacy_expense") == D("60")
        assert account_balance("accounts_payable") == D("60")
        assert net_worth() == D("-60")
        _assert_balanced()


def test_zero_cost_fill_accrues_nothing(make_tenant):
    with schema_context(make_tenant().schema_name):
        rx = _rx(cost="0", refills=2, days_supply=30)
        assert rx.bill_id is None
        assert rx.status == InvoiceStatus.PAID
        assert JournalEntry.objects.count() == 0
        assert net_worth() == ZERO


def test_next_refill_recomputed_on_save(make_tenant):
    with schema_context(make_tenant().schema_name):
        rx = _rx(cost="20", date=JAN, days_supply=30, refills=3)
        assert rx.next_refill_date == JAN + datetime.timedelta(days=30)


# --- HSA-funded fill -------------------------------------------------------------------------

def test_hsa_funded_fill_drops_hsa_and_holds_invariant(make_tenant):
    with schema_context(make_tenant().schema_name):
        from apps.investments.models import InvestmentTransaction

        hsa = _hsa(opening="5000")
        rx = _rx(cost="400")
        pay = record_prescription_payment(
            rx, amount=D("400"), date=FEB, funding=Funding.HSA, hsa=hsa
        )
        rx.refresh_from_db()
        assert rx.status == InvoiceStatus.PAID
        assert account_balance("accounts_payable") == ZERO          # AP settled directly
        assert account_balance(hsa.gl_account) == D("4600")         # HSA dropped by the fill
        assert account_balance("pharmacy_expense") == D("400")      # expense booked once
        wd = InvestmentTransaction.objects.get(account=hsa, txn_type="withdrawal")
        assert pay.hsa_txn_id == wd.pk and wd.amount == D("400")
        assert _hsa_inv(hsa)                                        # investments invariant intact
        assert net_worth() == D("4600")
        _assert_balanced()


# --- partial payments ------------------------------------------------------------------------

def test_partial_then_full_payment(make_tenant):
    with schema_context(make_tenant().schema_name):
        bank = _bank()
        rx = _rx(cost="90")
        record_prescription_payment(rx, amount=D("30"), date=JAN, funding=Funding.BANK,
                                    account=bank)
        rx.refresh_from_db()
        assert rx.status == InvoiceStatus.PARTIALLY_PAID
        assert rx.outstanding == D("60")
        record_prescription_payment(rx, amount=D("60"), date=FEB, funding=Funding.BANK,
                                    account=bank)
        rx.refresh_from_db()
        assert rx.status == InvoiceStatus.PAID
        assert rx.outstanding == ZERO
        assert account_balance("accounts_payable") == ZERO
        _assert_balanced()


def test_delete_refuses_on_foreign_payment(make_tenant):
    import pytest

    with schema_context(make_tenant().schema_name):
        from apps.payables.models import Payment
        from apps.payables.services import apply_payment

        pharmacy = _org()
        rx = _rx(pharmacy=pharmacy, cost="50")
        foreign = Payment.objects.create(
            vendor_organization=pharmacy, date=JAN, amount=D("20"),
            funding_kind=Payment.Funding.CASH,
        )
        apply_payment(foreign, [(rx.bill, D("20"))])
        with pytest.raises(ValueError):
            delete_prescription(rx, user=None)
        assert Prescription.all_objects.filter(pk=rx.pk, deleted_at__isnull=True).exists()


# --- reminders feed --------------------------------------------------------------------------

def test_reminders_due_orders_all_sources(make_tenant):
    with schema_context(make_tenant().schema_name):
        from apps.health.models import HealthPlan, ProviderInvoice
        from apps.insurance.models import InsurancePolicy, PolicyStatus, PolicyType

        today = datetime.date.today()

        # (1) an unpaid provider invoice due in 5 days
        inv = ProviderInvoice(
            biller_organization=_org("Clinic"), invoice_date=today,
            due_date=today + datetime.timedelta(days=5), amount_due=D("120"),
            status=InvoiceStatus.UNPAID,
        )
        inv.save()
        save_invoice(inv, is_new=True)

        # (2) a scheduled appointment in 10 days
        Encounter.objects.create(
            patient=_person("Appt", "Patient"), encounter_type=EncounterType.MEDICAL,
            visit_status=VisitStatus.SCHEDULED, date=today + datetime.timedelta(days=10),
        )

        # (3) a prescription refill coming due in 20 days
        _rx(cost="15", date=today, days_supply=20, refills=2)

        # (4) a plan-year / deductible reset in 30 days
        reset = today + datetime.timedelta(days=30)
        policy = InsurancePolicy.objects.create(
            policy_type=PolicyType.HEALTH, insurer_organization=_org("Aetna"),
            currency=_usd(), status=PolicyStatus.ACTIVE,
        )
        HealthPlan.objects.create(
            policy=policy, plan_year_start_month=reset.month, plan_year_start_day=reset.day,
            deductible_individual=D("1000"),
        )

        rows = reminders_due(within_days=90)
        kinds = [r["kind"] for r in rows]
        assert kinds == ["invoice", "appointment", "refill", "plan_year"]
        assert [r["days"] for r in rows] == [5, 10, 20, 30]
