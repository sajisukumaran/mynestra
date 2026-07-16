"""Health GL/service layer (Plan D, P1) — provider invoices route through Payables as locked bills
(+ optional locked partial payments) to the encounter-type expense account; encounters post nothing
themselves; the Pending-insurance → confirm → unpaid → partial → paid lifecycle; HSA funding (an
Investments WITHDRAWAL settling AP straight from the HSA, invariant intact); write-off; overpayment
+ refund; the duplicate-invoice warning; delete guards; the outstanding-by-provider read model."""

import datetime
from decimal import Decimal

from django_tenants.utils import schema_context

from apps.finance.models import JournalEntry
from apps.finance.services import account_balance, net_worth
from apps.health.models import Encounter, EncounterType, Funding, InvoiceStatus, ProviderInvoice
from apps.health.services import (
    confirm_invoice,
    delete_invoice,
    dispute_invoice,
    duplicate_warnings,
    outstanding_by_provider,
    record_invoice_payment,
    record_refund,
    save_invoice,
    total_unpaid,
    write_off_invoice,
)

D = Decimal
ZERO = D("0")
JAN = datetime.date(2026, 1, 15)
FEB = datetime.date(2026, 2, 15)


# --- helpers (inside schema_context) ---------------------------------------------------------

def _org(name="City Hospital"):
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


def _encounter(patient=None, etype=EncounterType.MEDICAL, **kw):
    enc = Encounter.objects.create(
        patient=patient or _person(), encounter_type=etype, date=JAN, **kw
    )
    return enc


def _invoice(encounter=None, *, biller=None, amount="100", status=InvoiceStatus.UNPAID,
             invoice_number="", invoice_date=JAN, save=True, user=None):
    inv = ProviderInvoice(
        encounter=encounter, biller_organization=biller, invoice_number=invoice_number,
        invoice_date=invoice_date, amount_due=D(amount), status=status,
    )
    inv.save()
    if save:
        save_invoice(inv, is_new=True, user=user)
    inv.refresh_from_db()
    return inv


def _hsa_inv(acct):
    """The invariant for a cash-only HSA: gl node == settlement cash (no lots)."""
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


# --- encounters + pending post nothing -------------------------------------------------------

def test_encounter_alone_posts_nothing(make_tenant):
    with schema_context(make_tenant().schema_name):
        _encounter()
        assert JournalEntry.objects.count() == 0
        assert net_worth() == ZERO


def test_pending_insurance_invoice_posts_nothing(make_tenant):
    with schema_context(make_tenant().schema_name):
        enc = _encounter()
        inv = _invoice(enc, biller=_org(), amount="250",
                       status=InvoiceStatus.PENDING_INSURANCE)
        assert inv.bill_id is None
        assert JournalEntry.objects.count() == 0
        assert net_worth() == ZERO
        enc.refresh_from_db()
        assert enc.total_outstanding == ZERO


def test_confirm_posts_expense_and_ap(make_tenant):
    with schema_context(make_tenant().schema_name):
        enc = _encounter()
        inv = _invoice(enc, biller=_org(), amount="250",
                       status=InvoiceStatus.PENDING_INSURANCE)
        confirm_invoice(inv, user=None)
        inv.refresh_from_db()
        assert inv.status == InvoiceStatus.UNPAID
        assert inv.bill is not None and inv.bill.is_locked
        assert account_balance("medical_expense") == D("250")
        assert account_balance("accounts_payable") == D("250")
        assert net_worth() == D("-250")  # accrued expense lifts a liability
        _assert_balanced()


def test_encounter_type_routes_to_its_account(make_tenant):
    with schema_context(make_tenant().schema_name):
        cases = [
            (EncounterType.DENTAL, "dental_expense"),
            (EncounterType.VISION, "vision_expense"),
            (EncounterType.HOSPITAL, "hospital_expense"),
        ]
        for etype, key in cases:
            enc = _encounter(etype=etype)
            _invoice(enc, biller=_org(f"{etype} biller"), amount="100")
            assert account_balance(key) == D("100")


# --- partial payments ------------------------------------------------------------------------

def test_partial_then_full_payment(make_tenant):
    with schema_context(make_tenant().schema_name):
        bank = _bank()
        inv = _invoice(_encounter(), biller=_org(), amount="300")
        record_invoice_payment(inv, amount=D("100"), date=JAN, funding=Funding.BANK, account=bank)
        inv.refresh_from_db()
        assert inv.status == InvoiceStatus.PARTIALLY_PAID
        assert inv.outstanding == D("200")
        record_invoice_payment(inv, amount=D("200"), date=FEB, funding=Funding.BANK, account=bank)
        inv.refresh_from_db()
        assert inv.status == InvoiceStatus.PAID
        assert inv.outstanding == ZERO
        assert account_balance("accounts_payable") == ZERO
        _assert_balanced()


def test_each_funding_source_settles_ap(make_tenant):
    with schema_context(make_tenant().schema_name):
        from apps.cards.models import CreditCard

        bank = _bank()
        card = CreditCard.objects.create(issuer=_org("Card Co"), nickname="Visa", currency=_usd())
        from apps.cards.services import ensure_gl_account as card_gl

        card_gl(card)
        cases = [
            (Funding.BANK, {"account": bank}),
            (Funding.CARD, {"card": card}),
            (Funding.CASH, {}),
        ]
        for funding, kw in cases:
            inv = _invoice(_encounter(), biller=_org(f"biller {funding}"), amount="100")
            record_invoice_payment(inv, amount=D("100"), date=JAN, funding=funding, **kw)
            inv.refresh_from_db()
            assert inv.status == InvoiceStatus.PAID, funding
        assert account_balance("accounts_payable") == ZERO
        _assert_balanced()


# --- HSA funding -----------------------------------------------------------------------------

def test_hsa_payment_settles_ap_and_drops_hsa(make_tenant):
    with schema_context(make_tenant().schema_name):
        from apps.investments.models import InvestmentTransaction

        hsa = _hsa(opening="5000")
        inv = _invoice(_encounter(), biller=_org(), amount="400")
        pay = record_invoice_payment(inv, amount=D("400"), date=FEB, funding=Funding.HSA, hsa=hsa)
        inv.refresh_from_db()

        assert inv.status == InvoiceStatus.PAID
        assert account_balance("accounts_payable") == ZERO       # AP settled directly
        assert account_balance(hsa.gl_account) == D("4600")      # HSA dropped by the payment
        assert account_balance("medical_expense") == D("400")    # expense booked once
        # A real Investments WITHDRAWAL backs it, linked to the locked payment.
        wd = InvestmentTransaction.objects.get(account=hsa, txn_type="withdrawal")
        assert pay.hsa_txn_id == wd.pk and wd.amount == D("400")
        assert _hsa_inv(hsa)                                     # investments invariant intact
        # Started with 5000 in the HSA (opening equity), spent 400 on healthcare → 4600 left.
        assert net_worth() == D("4600")
        _assert_balanced()


def test_hsa_payment_teardown_restores_balance(make_tenant):
    with schema_context(make_tenant().schema_name):
        from apps.health.services import delete_invoice_payment

        hsa = _hsa(opening="5000")
        inv = _invoice(_encounter(), biller=_org(), amount="400")
        pay = record_invoice_payment(inv, amount=D("400"), date=FEB, funding=Funding.HSA, hsa=hsa)
        delete_invoice_payment(inv, pay, user=None)
        inv.refresh_from_db()
        assert inv.status == InvoiceStatus.UNPAID
        assert account_balance(hsa.gl_account) == D("5000")      # HSA restored
        assert account_balance("accounts_payable") == D("400")   # bill re-accrued
        assert _hsa_inv(hsa)
        _assert_balanced()


# --- write-off -------------------------------------------------------------------------------

def test_write_off_reposts_bill_lower(make_tenant):
    with schema_context(make_tenant().schema_name):
        inv = _invoice(_encounter(), biller=_org(), amount="500")
        assert account_balance("medical_expense") == D("500")
        write_off_invoice(inv, new_total=D("300"), user=None)
        inv.refresh_from_db()
        assert inv.status == InvoiceStatus.WRITTEN_OFF
        assert inv.amount_due == D("300")
        assert account_balance("medical_expense") == D("300")    # dropped in place
        assert account_balance("accounts_payable") == D("300")
        _assert_balanced()


def test_full_write_off_unposts_bill(make_tenant):
    with schema_context(make_tenant().schema_name):
        inv = _invoice(_encounter(), biller=_org(), amount="500")
        write_off_invoice(inv, new_total=ZERO, user=None)
        inv.refresh_from_db()
        assert inv.status == InvoiceStatus.WRITTEN_OFF
        assert account_balance("medical_expense") == ZERO
        assert account_balance("accounts_payable") == ZERO
        assert net_worth() == ZERO
        _assert_balanced()


# --- overpayment + refund --------------------------------------------------------------------

def test_overpayment_leaves_ap_debit_then_refund_clears_it(make_tenant):
    with schema_context(make_tenant().schema_name):
        bank = _bank()
        inv = _invoice(_encounter(), biller=_org(), amount="100")
        record_invoice_payment(inv, amount=D("120"), date=JAN, funding=Funding.BANK, account=bank)
        inv.refresh_from_db()
        assert inv.status == InvoiceStatus.OVERPAID
        assert inv.refund_expected == D("20")
        assert account_balance("accounts_payable") == D("-20")   # biller owes you 20 (AP debit)

        record_refund(inv, amount=D("20"), dest=Funding.BANK, date=FEB, bank=bank)
        inv.refresh_from_db()
        assert inv.status == InvoiceStatus.PAID
        assert inv.refund_expected == ZERO
        assert account_balance("accounts_payable") == ZERO       # cleared
        _assert_balanced()


def test_refund_to_hsa(make_tenant):
    with schema_context(make_tenant().schema_name):
        hsa = _hsa(opening="5000")
        inv = _invoice(_encounter(), biller=_org(), amount="100")
        record_invoice_payment(inv, amount=D("150"), date=JAN, funding=Funding.HSA, hsa=hsa)
        inv.refresh_from_db()
        assert inv.status == InvoiceStatus.OVERPAID
        assert account_balance(hsa.gl_account) == D("4850")      # 5000 - 150 paid out
        record_refund(inv, amount=D("50"), dest=Funding.HSA, date=FEB, hsa=hsa)
        inv.refresh_from_db()
        assert inv.status == InvoiceStatus.PAID
        assert account_balance(hsa.gl_account) == D("4900")      # 50 returned to the HSA
        assert account_balance("accounts_payable") == ZERO
        assert _hsa_inv(hsa)
        _assert_balanced()


# --- itemized EOB ----------------------------------------------------------------------------

def test_itemized_charges_drive_amount_and_bill(make_tenant):
    with schema_context(make_tenant().schema_name):
        from apps.health.models import InvoiceCharge

        enc = _encounter()
        inv = ProviderInvoice(encounter=enc, biller_organization=_org(), invoice_date=JAN,
                              status=InvoiceStatus.UNPAID)
        inv.save()
        InvoiceCharge.objects.create(invoice=inv, description="Office visit", billed=D("200"),
                                     allowed=D("120"), insurance_paid=D("90"),
                                     copay_amount=D("30"), order=0)
        InvoiceCharge.objects.create(invoice=inv, description="Lab", billed=D("80"),
                                     allowed=D("50"), insurance_paid=D("0"),
                                     deductible_amount=D("50"), order=1)
        save_invoice(inv, is_new=True)
        inv.refresh_from_db()
        assert inv.amount_due == D("80")                         # 30 copay + 50 deductible
        assert inv.bill.lines.count() == 2                       # one line per charge
        assert account_balance("medical_expense") == D("80")
        _assert_balanced()


# --- disputes --------------------------------------------------------------------------------

def test_disputed_stays_accrued_but_out_of_owed(make_tenant):
    with schema_context(make_tenant().schema_name):
        inv = _invoice(_encounter(), biller=_org(), amount="100")
        dispute_invoice(inv, user=None)
        inv.refresh_from_db()
        assert inv.status == InvoiceStatus.DISPUTED
        assert account_balance("accounts_payable") == D("100")   # still accrued
        assert total_unpaid() == ZERO                            # excluded from you-owe
        _assert_balanced()


# --- duplicate detection ---------------------------------------------------------------------

def test_duplicate_warning_flags_matching_invoice(make_tenant):
    with schema_context(make_tenant().schema_name):
        biller = _org("Radiology Inc")
        _invoice(_encounter(), biller=biller, amount="100", invoice_number="A-1")
        dupe = ProviderInvoice(biller_organization=biller, invoice_number="A-1",
                               invoice_date=JAN, amount_due=D("100"))
        warnings = duplicate_warnings(dupe)
        assert warnings and warnings[0]["reason"] == "same biller and invoice number"


# --- delete guards ---------------------------------------------------------------------------

def test_delete_refuses_on_foreign_payment(make_tenant):
    import pytest

    with schema_context(make_tenant().schema_name):
        from apps.payables.models import Payment
        from apps.payables.services import apply_payment

        biller = _org()
        inv = _invoice(_encounter(), biller=biller, amount="100")
        # A payment recorded directly in Payables (not Health-sourced) allocated to the locked bill.
        foreign = Payment.objects.create(
            vendor_organization=biller, date=JAN, amount=D("40"),
            funding_kind=Payment.Funding.CASH,
        )
        apply_payment(foreign, [(inv.bill, D("40"))])
        with pytest.raises(ValueError):
            delete_invoice(inv, user=None)
        assert ProviderInvoice.all_objects.filter(pk=inv.pk, deleted_at__isnull=True).exists()


def test_delete_erases_bill_and_own_payments(make_tenant):
    with schema_context(make_tenant().schema_name):
        bank = _bank()
        inv = _invoice(_encounter(), biller=_org(), amount="100")
        record_invoice_payment(inv, amount=D("100"), date=JAN, funding=Funding.BANK, account=bank)
        bill_pk = inv.bill_id
        delete_invoice(inv, user=None)
        from apps.payables.models import Bill

        assert not Bill.all_objects.filter(pk=bill_pk).exists()
        assert account_balance("accounts_payable") == ZERO
        assert account_balance("medical_expense") == ZERO
        _assert_balanced()


# --- read models -----------------------------------------------------------------------------

def test_outstanding_by_provider_and_total(make_tenant):
    with schema_context(make_tenant().schema_name):
        a, b = _org("Provider A"), _org("Provider B")
        bank = _bank()
        _invoice(_encounter(), biller=a, amount="300")
        inv2 = _invoice(_encounter(), biller=b, amount="200")
        record_invoice_payment(inv2, amount=D("50"), date=JAN, funding=Funding.BANK, account=bank)
        _invoice(_encounter(), biller=a, amount="100")

        rows = outstanding_by_provider()
        assert rows[0]["name"] == "Provider A" and rows[0]["outstanding"] == D("400")
        assert rows[1]["name"] == "Provider B" and rows[1]["outstanding"] == D("150")
        assert total_unpaid() == D("550")


def test_encounter_rollups(make_tenant):
    with schema_context(make_tenant().schema_name):
        bank = _bank()
        enc = _encounter()
        _invoice(enc, biller=_org(), amount="300")
        inv2 = _invoice(enc, biller=_org("Lab"), amount="200")
        record_invoice_payment(inv2, amount=D("200"), date=JAN, funding=Funding.BANK, account=bank)
        enc.refresh_from_db()
        assert enc.total_patient_responsibility == D("500")
        assert enc.total_paid == D("200")
        assert enc.total_outstanding == D("300")
