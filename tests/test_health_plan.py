"""Health P2 — insurance cost-sharing + the deductible / OOP accumulator. Covers the dental/vision
premium homes (5550/5560), deductible_oop_status per person / family / embedded-individual, dental
annual-max + vision allowance, auto-linking an encounter to its active plan, and the invariant that
the HealthPlan overlay posts nothing."""

import datetime
from decimal import Decimal

from django_tenants.utils import schema_context

from apps.finance.models import JournalEntry
from apps.finance.services import account_balance, net_worth
from apps.health.models import (
    Encounter,
    EncounterType,
    InvoiceCharge,
    InvoiceStatus,
    ProviderInvoice,
)
from apps.health.services import (
    active_health_plan,
    deductible_oop_status,
    save_encounter,
    save_invoice,
)

D = Decimal
ZERO = D("0")
JAN = datetime.date(2026, 1, 15)


def _org(name="Aetna"):
    from apps.organizations.models import Organization

    return Organization.objects.create(name=name)


def _usd():
    from apps.finance.models import Currency

    return Currency.objects.get(code="USD")


def _person(first="Alice", last="Rivera"):
    from apps.contacts.models import Person

    return Person.objects.create(first_name=first, last_name=last)


def _health_policy(policy_type=None, *, members=(), **plan_kw):
    from apps.health.models import HealthPlan
    from apps.insurance.models import (
        InsurancePolicy,
        MemberRole,
        PolicyMember,
        PolicyStatus,
        PolicyType,
    )

    policy = InsurancePolicy.objects.create(
        policy_type=policy_type or PolicyType.HEALTH, insurer_organization=_org(),
        currency=_usd(), status=PolicyStatus.ACTIVE,
    )
    for i, person in enumerate(members):
        PolicyMember.objects.create(
            policy=policy, person=person,
            role=MemberRole.POLICYHOLDER if i == 0 else MemberRole.DEPENDENT,
        )
    if plan_kw is not None:
        HealthPlan.objects.create(policy=policy, **plan_kw)
    return policy


def _visit_with_charges(policy, patient, etype, charges, *, date=JAN):
    enc = Encounter.objects.create(patient=patient, encounter_type=etype, date=date, plan=policy)
    inv = ProviderInvoice(
        encounter=enc, biller_organization=_org("Clinic"), invoice_date=date,
        status=InvoiceStatus.UNPAID,
    )
    inv.save()
    for i, ch in enumerate(charges):
        InvoiceCharge.objects.create(
            invoice=inv, description=ch.get("desc", "Service"), order=i,
            billed=D(ch.get("billed", "0")), allowed=D(ch.get("allowed", "0")),
            insurance_paid=D(ch.get("insurance_paid", "0")),
            deductible_amount=D(ch.get("deductible", "0")), copay_amount=D(ch.get("copay", "0")),
            coinsurance_amount=D(ch.get("coinsurance", "0")),
            noncovered_amount=D(ch.get("noncovered", "0")),
            applies_to_deductible=ch.get("applies_ded", True),
            applies_to_oop=ch.get("applies_oop", True),
        )
    save_invoice(inv, is_new=True)
    return enc, inv


def _meters_by_person(status, kind):
    return {m["person"].pk: m for m in status["meters"] if m["kind"] == kind and m["person"]}


# --- premium homes ---------------------------------------------------------------------------

def test_dental_vision_premiums_route_to_new_accounts(make_tenant):
    with schema_context(make_tenant().schema_name):
        from apps.insurance.models import (
            InsurancePolicy,
            InsurancePremium,
            PolicyStatus,
            PolicyType,
        )
        from apps.insurance.services import save_premium

        for ptype, acct in [(PolicyType.DENTAL, "dental_insurance"),
                            (PolicyType.VISION, "vision_insurance")]:
            policy = InsurancePolicy.objects.create(
                policy_type=ptype, insurer_organization=_org(f"{ptype} co"),
                currency=_usd(), status=PolicyStatus.ACTIVE,
            )
            prem = InsurancePremium(policy=policy, date=JAN, amount=D("120"))
            prem.save()
            save_premium(prem, is_new=True)
            assert account_balance(acct) == D("120")


# --- deductible / OOP accumulator ------------------------------------------------------------

def test_deductible_oop_per_person_and_family(make_tenant):
    with schema_context(make_tenant().schema_name):
        alice, bob = _person("Alice"), _person("Bob")
        policy = _health_policy(
            members=[alice, bob],
            deductible_individual=D("1000"), deductible_family=D("2000"),
            oop_max_individual=D("3000"), oop_max_family=D("6000"),
        )
        _visit_with_charges(
            policy, alice, EncounterType.MEDICAL, [{"deductible": "400", "copay": "30"}]
        )
        status = deductible_oop_status(policy, as_of=JAN)

        assert status is not None
        assert status["family"]["deductible_used"] == D("400")
        assert status["family"]["oop_used"] == D("430")  # 400 deductible + 30 copay
        alice_ded = _meters_by_person(status, "deductible")[alice.pk]
        assert alice_ded["used"] == D("400") and alice_ded["limit"] == D("1000")
        assert _meters_by_person(status, "deductible")[bob.pk]["used"] == ZERO


def test_embedded_individual_met_via_family(make_tenant):
    with schema_context(make_tenant().schema_name):
        alice, bob = _person("Alice"), _person("Bob")
        policy = _health_policy(
            members=[alice, bob], deductible_individual=D("1000"), deductible_family=D("2000"),
        )
        _visit_with_charges(policy, alice, EncounterType.MEDICAL, [{"deductible": "1200"}])
        _visit_with_charges(policy, bob, EncounterType.MEDICAL, [{"deductible": "900"}])
        status = deductible_oop_status(policy, as_of=JAN)

        assert status["family"]["deductible_used"] == D("2100")  # >= 2000 family cap
        ded = _meters_by_person(status, "deductible")
        assert ded[alice.pk]["met"] is True   # own 1200 >= 1000
        assert ded[bob.pk]["met"] is True      # own 900 < 1000, but family met (embedded)


def test_dental_annual_max(make_tenant):
    with schema_context(make_tenant().schema_name):
        from apps.insurance.models import PolicyType

        alice = _person("Alice")
        policy = _health_policy(PolicyType.DENTAL, members=[alice], dental_annual_max=D("1500"))
        _visit_with_charges(
            policy, alice, EncounterType.DENTAL,
            [{"insurance_paid": "600"}, {"insurance_paid": "300"}],
        )
        status = deductible_oop_status(policy, as_of=JAN)
        assert status["dental"] is not None
        assert status["dental"]["used"] == D("900") and status["dental"]["limit"] == D("1500")


def test_vision_allowance(make_tenant):
    with schema_context(make_tenant().schema_name):
        from apps.insurance.models import PolicyType

        alice = _person("Alice")
        policy = _health_policy(PolicyType.VISION, members=[alice], vision_allowance=D("200"))
        _visit_with_charges(policy, alice, EncounterType.VISION, [{"insurance_paid": "150"}])
        status = deductible_oop_status(policy, as_of=JAN)
        assert status["vision"]["used"] == D("150") and status["vision"]["limit"] == D("200")


def test_status_none_without_healthplan(make_tenant):
    with schema_context(make_tenant().schema_name):
        from apps.insurance.models import InsurancePolicy, PolicyStatus, PolicyType

        policy = InsurancePolicy.objects.create(
            policy_type=PolicyType.HEALTH, insurer_organization=_org(), currency=_usd(),
            status=PolicyStatus.ACTIVE,
        )
        assert deductible_oop_status(policy, as_of=JAN) is None


def test_healthplan_overlay_posts_nothing(make_tenant):
    with schema_context(make_tenant().schema_name):
        alice = _person("Alice")
        _health_policy(members=[alice], deductible_individual=D("1000"))
        assert JournalEntry.objects.count() == 0  # the cost-sharing overlay posts nothing
        assert net_worth() == ZERO


# --- auto-link -------------------------------------------------------------------------------

def test_active_plan_auto_links_encounter(make_tenant):
    with schema_context(make_tenant().schema_name):
        alice = _person("Alice")
        policy = _health_policy(members=[alice])
        # active_health_plan resolves the covering policy for a medical visit.
        found = active_health_plan(alice, on_date=JAN, encounter_type=EncounterType.MEDICAL)
        assert found == policy
        # save_encounter auto-links it.
        enc = Encounter.objects.create(
            patient=alice, encounter_type=EncounterType.MEDICAL, date=JAN
        )
        save_encounter(enc, is_new=True)
        enc.refresh_from_db()
        assert enc.plan_id == policy.pk
        # A dental visit for the same patient with only a health plan does NOT link.
        dental = Encounter.objects.create(
            patient=alice, encounter_type=EncounterType.DENTAL, date=JAN
        )
        save_encounter(dental, is_new=True)
        dental.refresh_from_db()
        assert dental.plan_id is None
