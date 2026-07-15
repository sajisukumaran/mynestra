"""Insurance GL/service layer — premiums route through Payables as locked bills (+ optional locked
payments) to the per-type expense account; policies post nothing themselves; covers_through advances
the renewal date; void/delete lifecycle; the 'insured' P2O sync."""

import datetime
from decimal import Decimal

import pytest
from django_tenants.utils import schema_context

from apps.finance.models import JournalEntry
from apps.finance.services import account_balance, net_worth
from apps.insurance.models import (
    Funding,
    InsurancePolicy,
    InsurancePremium,
    MemberRole,
    PolicyMember,
    PolicyStatus,
    PolicyType,
)
from apps.insurance.services import (
    delete_premium,
    save_premium,
    sync_policy_p2o,
    void_premium,
)

D = Decimal
ZERO = D("0")
JAN = datetime.date(2026, 1, 15)


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


def _person(first="Sam", last="Rivera"):
    from apps.contacts.models import Person

    return Person.objects.create(first_name=first, last_name=last)


def _policy(policy_type=PolicyType.AUTO, *, insurer=None, **kw):
    defaults = {
        "policy_type": policy_type,
        "insurer_organization": insurer or _org(),
        "currency": _usd(),
        "status": PolicyStatus.ACTIVE,
    }
    defaults.update(kw)
    return InsurancePolicy.objects.create(**defaults)


def _premium(policy, amount, *, funding=Funding.NONE, account=None, covers_through=None,
             date=JAN, save=True):
    prem = InsurancePremium(
        policy=policy, date=date, amount=D(amount), funding_source=funding,
        funding_account=account, covers_through=covers_through,
    )
    prem.save()
    if save:
        save_premium(prem, is_new=True)
    return prem


# --- policies post nothing -------------------------------------------------------------------

def test_policy_alone_posts_nothing(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        _policy()
        assert JournalEntry.objects.count() == 0
        assert net_worth() == ZERO


# --- accrued premium -------------------------------------------------------------------------

def test_accrued_auto_premium_posts_locked_bill_to_5340(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        policy = _policy(PolicyType.AUTO)
        prem = _premium(policy, "1200")
        assert prem.bill is not None and prem.bill.is_locked and prem.bill.status == "open"
        assert prem.payment_id is None  # unpaid → no funding payment
        # Auto premiums keep the vehicle-insurance home (5340).
        assert account_balance("vehicle_insurance") == D("1200")
        assert account_balance("accounts_payable") == D("1200")
        # An accrued expense lifts a liability → net worth falls by the premium.
        assert net_worth() == D("-1200")


def test_health_premium_posts_to_5510(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        policy = _policy(PolicyType.HEALTH)
        _premium(policy, "500")
        assert account_balance("health_insurance") == D("500")
        assert account_balance("vehicle_insurance") == ZERO


def test_type_accounts_route_per_policy_type(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        cases = [
            (PolicyType.HOME, "home_insurance"),
            (PolicyType.RENTERS, "renters_insurance"),
            (PolicyType.LIFE, "life_insurance"),
            (PolicyType.UMBRELLA, "umbrella_insurance"),
            (PolicyType.PET, "other_insurance"),
        ]
        for ptype, key in cases:
            _premium(_policy(ptype), "100")
            assert account_balance(key) == D("100"), key


# --- funded premium --------------------------------------------------------------------------

def test_bank_funded_premium_creates_payment_and_pays_bill(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        bank = _bank()
        policy = _policy(PolicyType.AUTO)
        prem = _premium(policy, "900", funding=Funding.BANK, account=bank)
        assert prem.payment is not None and prem.payment.is_locked
        assert prem.bill.status == "paid"
        assert account_balance("vehicle_insurance") == D("900")
        # AP nets to zero (accrued then paid); the asset (bank) fell → net worth −900.
        assert account_balance("accounts_payable") == ZERO
        assert net_worth() == D("-900")


# --- renewal advance -------------------------------------------------------------------------

def test_covers_through_advances_policy_expiry(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        policy = _policy(PolicyType.AUTO)
        through = datetime.date(2027, 1, 14)
        _premium(policy, "1200", covers_through=through)
        policy.refresh_from_db()
        assert policy.expiry_date == through


# --- lifecycle: void keeps, delete erases ----------------------------------------------------

def test_void_premium_reverses_but_keeps_record(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        policy = _policy(PolicyType.AUTO)
        prem = _premium(policy, "1200")
        void_premium(prem)
        assert account_balance("vehicle_insurance") == ZERO
        assert account_balance("accounts_payable") == ZERO
        assert InsurancePremium.objects.filter(pk=prem.pk).exists()  # record kept


def test_delete_premium_hard_erases(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        policy = _policy(PolicyType.AUTO)
        prem = _premium(policy, "1200")
        delete_premium(prem)
        assert InsurancePremium.objects.count() == 0
        assert account_balance("vehicle_insurance") == ZERO
        assert account_balance("accounts_payable") == ZERO


def test_delete_premium_refuses_foreign_payment(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        from apps.payables.models import Payment
        from apps.payables.services import apply_payment

        policy = _policy(PolicyType.AUTO)
        prem = _premium(policy, "1200")  # accrued (unpaid) locked bill
        # A payment recorded in Payables (foreign to the insurance module) settles the bill.
        foreign = Payment(
            vendor_organization=policy.insurer_organization, date=JAN, amount=D("1200"),
            funding_kind=Payment.Funding.CASH,
        )
        foreign.save()
        apply_payment(foreign, [(prem.bill, D("1200"))])
        with pytest.raises(ValueError):
            delete_premium(prem)


# --- covered-person P2O sync -----------------------------------------------------------------

def test_sync_policy_p2o_links_insured_idempotently(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        from apps.relationships.models import PersonOrgRelationship

        policy = _policy(PolicyType.AUTO)
        person = _person()
        PolicyMember.objects.create(policy=policy, person=person, role=MemberRole.INSURED)
        sync_policy_p2o(policy)
        sync_policy_p2o(policy)  # idempotent
        links = PersonOrgRelationship.objects.filter(
            person=person, organization=policy.insurer_organization, type__code="insured"
        )
        assert links.count() == 1
