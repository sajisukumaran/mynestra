"""Insurance screens (authenticated tenant client): dashboard, list, policy create (with insurer /
coverage / member / covered vehicle), record a funded premium, the Payables locked-bill/payment
read-only guards, and the Vehicle read-through card."""

import datetime
from decimal import Decimal

from django_tenants.utils import schema_context

from apps.tenants.models import Membership, Role

D = Decimal


def _owner(make_tenant, make_user, name="Insurance Household", email="owner@ins.test"):
    tenant = make_tenant(name=name)
    owner = make_user(email)
    Membership.objects.create(user=owner, tenant=tenant, role=Role.OWNER)
    return tenant, owner


def _org(name="Acme Insurance"):
    from apps.organizations.models import Organization

    return Organization.objects.create(name=name)


def _usd():
    from apps.finance.models import Currency

    return Currency.objects.get(code="USD")


def _bank(nickname="Checking"):
    from apps.banking.models import AccountType as BAT
    from apps.banking.models import BankAccount
    from apps.banking.services import ensure_gl_account as bank_gl

    acct = BankAccount.objects.create(
        bank=_org("My Bank"), account_type=BAT.CHECKING, nickname=nickname, currency=_usd()
    )
    bank_gl(acct)
    return acct


def _funded_premium(policy_type="auto"):
    """Create a bank-funded premium via the service (for the read-only-guard test)."""
    from apps.insurance.models import Funding, InsurancePolicy, InsurancePremium, PolicyType
    from apps.insurance.services import save_premium

    policy = InsurancePolicy.objects.create(
        policy_type=PolicyType.AUTO, insurer_organization=_org(), currency=_usd()
    )
    prem = InsurancePremium(
        policy=policy, date=datetime.date(2026, 1, 15), amount=D("900"),
        funding_source=Funding.BANK, funding_account=_bank(),
    )
    prem.save()
    save_premium(prem, is_new=True)
    return policy, prem


# --- screens render --------------------------------------------------------------------------

def test_dashboard_and_list_render(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        from apps.insurance.models import InsurancePolicy, PolicyType

        InsurancePolicy.objects.create(
            policy_type=PolicyType.HEALTH, insurer_organization=_org("Blue Shield"),
            currency=_usd(), nickname="Family Health",
        )
    client.force_login(owner)
    body = client.get(f"/t/{tenant.schema_name}/insurance/").content.decode()
    assert "Active policies" in body and "Annual premium" in body
    lst = client.get(f"/t/{tenant.schema_name}/insurance/policies/").content.decode()
    assert "Family Health" in lst


def test_policy_detail_and_forms_render(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        from apps.insurance.models import (
            InsurancePolicy,
            InsurancePremium,
            PolicyCoverage,
            PolicyType,
        )
        from apps.insurance.services import save_premium

        policy = InsurancePolicy.objects.create(
            policy_type=PolicyType.AUTO, insurer_organization=_org(), currency=_usd(),
            nickname="Family Auto", expiry_date=datetime.date(2027, 1, 1),
        )
        PolicyCoverage.objects.create(policy=policy, coverage_type="Liability")
        prem = InsurancePremium(policy=policy, date=datetime.date(2026, 1, 5), amount=D("1200"))
        prem.save()
        save_premium(prem, is_new=True)
        pid = policy.pk
    client.force_login(owner)
    base = f"/t/{tenant.schema_name}/insurance"
    detail = client.get(f"{base}/policies/{pid}/")
    assert detail.status_code == 200
    body = detail.content.decode()
    assert "Family Auto" in body and "Liability" in body and "Premiums" in body
    assert client.get(f"{base}/policies/new/").status_code == 200
    assert client.get(f"{base}/policies/{pid}/edit/").status_code == 200


# --- policy create ---------------------------------------------------------------------------

def test_create_auto_policy_with_coverage_member_and_vehicle(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        from apps.automobile.models import OwnershipMode, Vehicle
        from apps.contacts.models import Person

        person = Person.objects.create(
            first_name="Sam", last_name="Rivera", is_household_member=True
        )
        vehicle = Vehicle.objects.create(
            nickname="Family SUV", ownership_mode=OwnershipMode.OWNED_CASH, currency=_usd()
        )
        pid, vid = person.pk, vehicle.pk
    client.force_login(owner)
    resp = client.post(
        f"/t/{tenant.schema_name}/insurance/policies/new/",
        {
            "policy_type": "auto", "status": "active", "currency": "USD",
            "insurer_organization": "", "insurer_organization_new_name": "Acme Insurance",
            "nickname": "Family Auto", "plan_name": "", "policy_number": "POL-1",
            "effective_date": "2026-01-01", "expiry_date": "2027-01-01",
            "premium_amount": "1200", "premium_frequency": "annual",
            "coverage_type": "Liability", "coverage_limit": "100000",
            "coverage_deductible": "500", "coverage_premium": "", "coverage_note": "",
            "member_person": str(pid), "member_role": "insured",
            "member_percent": "", "member_note": "",
            "covered_vehicle": str(vid),
            "notes": "",
        },
    )
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        from apps.insurance.models import InsurancePolicy
        from apps.relationships.models import PersonOrgRelationship

        policy = InsurancePolicy.objects.get(nickname="Family Auto")
        assert policy.insurer_organization is not None  # inline-created insurer
        assert policy.coverages.count() == 1
        assert policy.members.filter(person_id=pid, role="insured").exists()
        assert policy.assets.filter(object_id=vid).count() == 1
        # The covered member is linked to the insurer via the 'insured' P2O type.
        assert PersonOrgRelationship.objects.filter(
            person_id=pid, organization=policy.insurer_organization, type__code="insured"
        ).exists()


# --- record a premium via the client ---------------------------------------------------------

def test_record_bank_funded_premium(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        from apps.insurance.models import InsurancePolicy, PolicyType

        policy = InsurancePolicy.objects.create(
            policy_type=PolicyType.AUTO, insurer_organization=_org(), currency=_usd()
        )
        bank = _bank()
        pid, bank_id = policy.pk, bank.pk
    client.force_login(owner)
    resp = client.post(
        f"/t/{tenant.schema_name}/insurance/policies/{pid}/premiums/new/",
        {
            "date": "2026-02-01", "amount": "1200", "funding_source": "bank",
            "funding_account": str(bank_id), "reference": "INV-9",
        },
    )
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        from apps.finance.services import account_balance
        from apps.insurance.models import InsurancePremium

        prem = InsurancePremium.objects.get(policy_id=pid)
        assert prem.bill is not None and prem.bill.status == "paid"
        assert prem.payment is not None and prem.payment.is_locked
        assert account_balance("vehicle_insurance") == D("1200")


# --- payables read-only guards (the lock seam) -----------------------------------------------

def test_locked_bill_and_payment_are_readonly_in_payables(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        policy, prem = _funded_premium()
        bill_id, pay_id, policy_id = prem.bill_id, prem.payment_id, policy.pk
    client.force_login(owner)
    assert client.get(f"/t/{tenant.schema_name}/payables/bills/{bill_id}/edit/").status_code == 403
    assert (
        client.get(f"/t/{tenant.schema_name}/payables/payments/{pay_id}/edit/").status_code == 403
    )
    assert (
        client.post(f"/t/{tenant.schema_name}/payables/payments/{pay_id}/delete/").status_code
        == 403
    )
    detail = client.get(f"/t/{tenant.schema_name}/payables/bills/{bill_id}/").content.decode()
    assert "Managed elsewhere" in detail
    assert f"insurance/policies/{policy_id}/" in detail


# --- vehicle read-through --------------------------------------------------------------------

def test_vehicle_detail_reads_through_to_policy(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        from apps.automobile.models import OwnershipMode, Vehicle
        from apps.insurance.models import InsurancePolicy, PolicyType
        from apps.insurance.services import set_covered_vehicles

        vehicle = Vehicle.objects.create(
            nickname="Family SUV", ownership_mode=OwnershipMode.OWNED_CASH, currency=_usd()
        )
        policy = InsurancePolicy.objects.create(
            policy_type=PolicyType.AUTO, insurer_organization=_org(), currency=_usd(),
            nickname="Family Auto",
        )
        set_covered_vehicles(policy, [vehicle])
        vid, policy_id = vehicle.pk, policy.pk
    client.force_login(owner)
    body = client.get(f"/t/{tenant.schema_name}/automobile/{vid}/").content.decode()
    assert "Family Auto" in body
    assert f"insurance/policies/{policy_id}/" in body
