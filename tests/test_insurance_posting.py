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


# --- Claims (Phase 2) ------------------------------------------------------------------------

def _resolve(code):
    from apps.finance.services import resolve_account

    return resolve_account(code)


def _book_loss(amount, *, account="5320", date=JAN):
    """Book a loss to an expense account (Dr expense / Cr AP) — the thing a claim reimburses."""
    from apps.finance.services import LineInput, post_entry

    post_entry(
        date=date,
        lines=[
            LineInput(account, debit=D(amount)),
            LineInput("accounts_payable", credit=D(amount)),
        ],
        description="Repair loss",
    )


def _assert_balanced():
    from django.db.models import Sum

    from apps.finance.models import JournalEntry, JournalLine

    agg = JournalLine.objects.filter(entry__status=JournalEntry.Status.POSTED).aggregate(
        d=Sum("base_debit"), c=Sum("base_credit")
    )
    assert agg["d"] == agg["c"], (agg["d"], agg["c"])


def _owned_vehicle(cost="30000"):
    from apps.automobile.models import CostKind, OwnershipMode, Vehicle, VehicleCostEvent
    from apps.automobile.services import save_cost_event

    v = Vehicle.objects.create(
        nickname="Family SUV", ownership_mode=OwnershipMode.OWNED_CASH, currency=_usd()
    )
    ev = VehicleCostEvent(
        vehicle=v, kind=CostKind.PURCHASE, date=JAN, amount=D(cost),
        vendor_organization=_org("Dealer"),
    )
    ev.save()
    save_cost_event(ev, is_new=True)
    v.refresh_from_db()
    return v


def _claim(policy, *, settlement="reimbursement", payout="0", deductible="0", destination="none",
           bank=None, cash=None, loss_account=None, vehicle=None, status="open",
           loss_date=JAN, payout_date=JAN, save=True):
    from apps.insurance.models import Claim
    from apps.insurance.services import save_claim, set_claim_vehicle

    claim = Claim(
        policy=policy, loss_date=loss_date, status=status, settlement_kind=settlement,
        deductible_amount=D(deductible), payout_amount=D(payout), payout_date=payout_date,
        payout_destination=destination, bank_account=bank, cash_account=cash,
        loss_expense_account=loss_account,
    )
    if vehicle is not None:
        set_claim_vehicle(claim, vehicle)
    claim.save()
    if save:
        save_claim(claim, is_new=True)
    return claim


def test_reimbursement_bank_nets_loss_expense_to_deductible(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        from apps.banking.models import BankTransaction, TxnType

        bank = _bank()
        _book_loss("5000")
        assert account_balance("5320") == D("5000")
        policy = _policy(PolicyType.AUTO)
        claim = _claim(
            policy, payout="4500", deductible="500", destination="bank",
            bank=bank, loss_account=_resolve("5320"),
        )
        assert claim.journal_entry_id is not None
        # The payout credits the loss expense → net retained expense = the deductible.
        assert account_balance("5320") == D("500")
        # Bank money-in routes via 1150 (nets to zero) + a native deposit in the register.
        assert account_balance("transfer_clearing") == ZERO
        assert claim.bank_txn_id is not None
        assert BankTransaction.objects.filter(
            account=bank, txn_type=TxnType.TRANSFER_IN, amount=D("4500")
        ).exists()
        _assert_balanced()


def test_reimbursement_cash_credits_expense_directly(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        _book_loss("3000")
        policy = _policy(PolicyType.AUTO)
        claim = _claim(
            policy, payout="2500", deductible="500", destination="cash",
            loss_account=_resolve("5320"),
        )
        assert claim.journal_entry_id is not None and claim.bank_txn_id is None
        assert account_balance("5320") == D("500")
        assert account_balance("1110") == D("2500")  # cash on hand rose by the payout
        _assert_balanced()


def test_open_claim_without_payout_posts_nothing(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        policy = _policy(PolicyType.HEALTH)
        claim = _claim(policy, payout="0", loss_account=_resolve("5320"), payout_date=None)
        assert claim.journal_entry_id is None
        assert JournalEntry.objects.count() == 0
        assert net_worth() == ZERO


def test_total_loss_creates_disposal_and_books_gain_loss(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        bank = _bank()
        vehicle = _owned_vehicle("30000")
        assert account_balance(vehicle.gl_account) == D("30000")
        policy = _policy(PolicyType.AUTO)
        claim = _claim(
            policy, settlement="total_loss", payout="20000", deductible="1000",
            destination="bank", bank=bank, vehicle=vehicle,
        )
        # The disposal owns the entry; the claim posts nothing of its own (double-book guard).
        assert claim.disposal_id is not None and claim.journal_entry_id is None
        vehicle.refresh_from_db()
        assert vehicle.is_active is False               # flipped disposed
        assert account_balance(vehicle.gl_account) == ZERO  # node derecognized
        assert claim.gain_loss == D("-10000")           # 20000 proceeds − 30000 cost = loss
        assert account_balance("transfer_clearing") == ZERO
        _assert_balanced()


def test_reimbursement_edit_reposts_to_new_amount(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        from apps.insurance.services import save_claim

        _book_loss("5000")
        policy = _policy(PolicyType.AUTO)
        claim = _claim(policy, payout="4500", destination="cash", loss_account=_resolve("5320"))
        assert account_balance("5320") == D("500")
        claim.payout_amount = D("4000")
        claim.save()
        save_claim(claim, is_new=False)
        claim.refresh_from_db()
        assert claim.posting_version == 2
        assert account_balance("5320") == D("1000")     # 5000 − 4000
        _assert_balanced()


def test_void_reimbursement_reverses_but_keeps_record(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        from apps.insurance.models import Claim
        from apps.insurance.services import void_claim

        _book_loss("5000")
        policy = _policy(PolicyType.AUTO)
        claim = _claim(policy, payout="4500", destination="bank", bank=_bank(),
                       loss_account=_resolve("5320"))
        void_claim(claim)
        assert account_balance("5320") == D("5000")      # payout undone
        assert account_balance("transfer_clearing") == ZERO
        assert Claim.objects.filter(pk=claim.pk).exists()  # record kept


def test_delete_reimbursement_hard_erases(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        from apps.insurance.models import Claim
        from apps.insurance.services import delete_claim

        _book_loss("5000")
        policy = _policy(PolicyType.AUTO)
        claim = _claim(policy, payout="4500", destination="cash", loss_account=_resolve("5320"))
        delete_claim(claim)
        assert Claim.objects.count() == 0
        assert account_balance("5320") == D("5000")      # only the original loss remains
        _assert_balanced()


def test_void_total_loss_restores_vehicle(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        from apps.insurance.models import Claim
        from apps.insurance.services import void_claim

        vehicle = _owned_vehicle("30000")
        policy = _policy(PolicyType.AUTO)
        claim = _claim(policy, settlement="total_loss", payout="20000", vehicle=vehicle)
        void_claim(claim)
        vehicle.refresh_from_db()
        assert vehicle.is_active is True                 # restored
        assert account_balance(vehicle.gl_account) == D("30000")  # cost re-recognized
        assert Claim.objects.filter(pk=claim.pk).exists()


def test_delete_total_loss_erases_disposal_and_restores_vehicle(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        from apps.automobile.models import VehicleDisposal
        from apps.insurance.models import Claim
        from apps.insurance.services import delete_claim

        vehicle = _owned_vehicle("30000")
        policy = _policy(PolicyType.AUTO)
        claim = _claim(policy, settlement="total_loss", payout="20000", vehicle=vehicle)
        delete_claim(claim)
        vehicle.refresh_from_db()
        assert vehicle.is_active is True
        assert account_balance(vehicle.gl_account) == D("30000")
        assert Claim.objects.count() == 0
        assert VehicleDisposal.objects.count() == 0
        _assert_balanced()
