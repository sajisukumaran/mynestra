"""Health — medical insurance claims (Explanation of Benefits).

A claim posts nothing itself. When it's PROCESSED / DENIED and there's a patient responsibility, it
materializes a locked ProviderInvoice (mirroring its lines) for exactly what you owe — so the money
flows through Payables and feeds deductible / OOP tracking. RECEIVED / IN_PROCESS post nothing; a
fully-covered claim leaves no bill; editing reprices the bill; deleting a claim tears its bill down;
and the EOB waterfall totals are derived from the lines."""

import datetime
from decimal import Decimal

from django_tenants.utils import schema_context

from apps.finance.models import JournalEntry
from apps.finance.services import account_balance, net_worth
from apps.health.models import ClaimLine, ClaimStatus, MedicalClaim
from apps.health.services import deductible_oop_status, delete_claim, save_claim

D = Decimal
ZERO = D("0")
JAN = datetime.date(2026, 1, 15)


def _org(name="City Hospital"):
    from apps.organizations.models import Organization

    return Organization.objects.create(name=name)


def _usd():
    from apps.finance.models import Currency

    return Currency.objects.get(code="USD")


def _person(first="Sam", last="Rivera"):
    from apps.contacts.models import Person

    return Person.objects.create(first_name=first, last_name=last)


def _assert_balanced():
    from django.db.models import Sum

    from apps.finance.models import JournalLine

    agg = JournalLine.objects.filter(entry__status=JournalEntry.Status.POSTED).aggregate(
        d=Sum("base_debit"), c=Sum("base_credit")
    )
    assert agg["d"] == agg["c"], (agg["d"], agg["c"])


def _claim(*, patient=None, provider=None, status=ClaimStatus.PROCESSED, lines=(),
           plan=None, encounter=None, number="CLM-1", save=True):
    claim = MedicalClaim(
        patient=patient or _person(), provider_organization=provider or _org(),
        claim_number=number, service_date=JAN, status=status, plan=plan, encounter=encounter,
    )
    claim.save()
    for i, line in enumerate(lines):
        ClaimLine.objects.create(
            claim=claim, order=i, description=line.get("desc", "Service"),
            service_code=line.get("code", ""), billed=D(line.get("billed", "0")),
            plan_discount=D(line.get("discount", "0")), allowed=D(line.get("allowed", "0")),
            plan_paid=D(line.get("plan_paid", "0")), deductible=D(line.get("deductible", "0")),
            copay=D(line.get("copay", "0")), coinsurance=D(line.get("coinsurance", "0")),
            not_covered=D(line.get("not_covered", "0")), remark_codes=line.get("remarks", ""),
        )
    if save:
        save_claim(claim, is_new=True)
    claim.refresh_from_db()
    return claim


# --- the EOB waterfall + generated bill ------------------------------------------------------

def test_processed_claim_generates_patient_responsibility_bill(make_tenant):
    with schema_context(make_tenant().schema_name):
        claim = _claim(lines=[
            {"desc": "Office visit", "billed": "200", "discount": "80", "allowed": "120",
             "plan_paid": "90", "copay": "30"},
            {"desc": "Lab", "billed": "100", "discount": "40", "allowed": "60",
             "plan_paid": "0", "deductible": "60"},
        ])
        # waterfall totals derived from the lines
        assert claim.total_billed == D("300")
        assert claim.total_plan_discount == D("120")
        assert claim.total_allowed == D("180")
        assert claim.total_plan_paid == D("90")
        assert claim.total_patient_responsibility == D("90")  # 30 copay + 60 deductible
        # a locked bill for exactly what you owe, mirroring the lines
        assert claim.invoice_id is not None
        assert claim.invoice.bill.is_locked
        assert claim.invoice.amount_due == D("90")
        assert claim.invoice.charges.count() == 2
        assert account_balance("medical_expense") == D("90")
        assert account_balance("accounts_payable") == D("90")
        assert net_worth() == D("-90")
        _assert_balanced()


def test_received_claim_posts_nothing(make_tenant):
    with schema_context(make_tenant().schema_name):
        claim = _claim(status=ClaimStatus.RECEIVED,
                       lines=[{"desc": "Visit", "billed": "200", "copay": "30"}])
        assert claim.invoice_id is None
        assert JournalEntry.objects.count() == 0
        assert net_worth() == ZERO


def test_fully_covered_claim_no_bill(make_tenant):
    with schema_context(make_tenant().schema_name):
        claim = _claim(lines=[{"desc": "Wellness", "billed": "200", "discount": "80",
                               "allowed": "120", "plan_paid": "120"}])
        assert claim.total_patient_responsibility == ZERO
        assert claim.invoice_id is None
        assert account_balance("accounts_payable") == ZERO


def test_denied_claim_bills_full_amount(make_tenant):
    with schema_context(make_tenant().schema_name):
        claim = _claim(status=ClaimStatus.DENIED,
                       lines=[{"desc": "MRI", "billed": "800", "not_covered": "800"}])
        assert claim.total_patient_responsibility == D("800")
        assert claim.invoice_id is not None
        assert account_balance("accounts_payable") == D("800")
        assert account_balance("medical_expense") == D("800")
        _assert_balanced()


def test_edit_claim_reprices_bill(make_tenant):
    with schema_context(make_tenant().schema_name):
        claim = _claim(lines=[{"desc": "Visit", "billed": "200", "copay": "30"}])
        assert account_balance("medical_expense") == D("30")
        claim.lines.all().delete()
        ClaimLine.objects.create(claim=claim, order=0, description="Visit",
                                 billed=D("200"), copay=D("50"))
        save_claim(claim, is_new=False)
        claim.refresh_from_db()
        assert claim.total_patient_responsibility == D("50")
        assert account_balance("medical_expense") == D("50")
        assert account_balance("accounts_payable") == D("50")
        _assert_balanced()


def test_delete_claim_tears_down_bill(make_tenant):
    with schema_context(make_tenant().schema_name):
        from apps.payables.models import Bill

        claim = _claim(lines=[{"desc": "Visit", "billed": "200", "copay": "40"}])
        bill_pk = claim.invoice.bill_id
        delete_claim(claim, user=None)
        assert not Bill.all_objects.filter(pk=bill_pk).exists()
        assert account_balance("accounts_payable") == ZERO
        assert account_balance("medical_expense") == ZERO
        assert not MedicalClaim.all_objects.filter(pk=claim.pk, deleted_at__isnull=True).exists()
        _assert_balanced()


# --- claims feed deductible / OOP (even standalone, via the claim's plan) ---------------------

def test_claim_feeds_deductible_oop(make_tenant):
    with schema_context(make_tenant().schema_name):
        from apps.health.models import HealthPlan
        from apps.insurance.models import (
            InsurancePolicy,
            MemberRole,
            PolicyMember,
            PolicyStatus,
            PolicyType,
        )

        alice = _person("Alice")
        policy = InsurancePolicy.objects.create(
            policy_type=PolicyType.HEALTH, insurer_organization=_org("Aetna"),
            currency=_usd(), status=PolicyStatus.ACTIVE,
        )
        PolicyMember.objects.create(policy=policy, person=alice, role=MemberRole.POLICYHOLDER)
        HealthPlan.objects.create(
            policy=policy, deductible_individual=D("1000"), oop_max_individual=D("3000")
        )
        _claim(patient=alice, plan=policy, lines=[
            {"desc": "Visit", "billed": "200", "allowed": "120", "plan_paid": "70",
             "deductible": "20", "copay": "30"},
        ])
        status = deductible_oop_status(policy, as_of=JAN)
        persons = {p["person"].pk: p for p in status["persons"]}
        assert persons[alice.pk]["deductible_used"] == D("20")
        assert persons[alice.pk]["oop_used"] == D("50")  # 20 deductible + 30 copay
