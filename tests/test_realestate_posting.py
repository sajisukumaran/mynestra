"""Real Estate GL/service layer — owned property capitalizes to a 1410.NN node; cost events route
through Payables as locked bills; property tax posts to 5810 (never the 5140 escrow home tax);
a financed purchase settles via a mortgage disbursement; a disposal books gain/loss to 4930 and
derecognizes the node without touching the mortgage; valuations post nothing."""

import datetime
from decimal import Decimal

from django_tenants.utils import schema_context

from apps.finance.services import account_balance, net_worth
from apps.realestate.models import (
    CostKind,
    DisposalMethod,
    Funding,
    OwnershipMode,
    Property,
    PropertyCostEvent,
    PropertyDisposal,
    PropertyValuation,
)
from apps.realestate.services import (
    post_disposal,
    save_cost_event,
    settle_financed_purchase,
)

D = Decimal
ZERO = D("0")
JAN = datetime.date(2026, 1, 15)
MAR = datetime.date(2026, 3, 1)


def _org(name="Seller Co"):
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


def _mortgage_loan(nickname="Mortgage"):
    from apps.loans.models import Loan, LoanType

    return Loan.objects.create(
        loan_type=LoanType.MORTGAGE, nickname=nickname, currency=_usd(),
        lender_organization=_org("Mortgage Lender"),
    )


def _property(ownership=OwnershipMode.OWNED_CASH, **kw):
    defaults = {"nickname": "Family Home", "ownership_mode": ownership, "currency": _usd()}
    defaults.update(kw)
    return Property.objects.create(**defaults)


def _event(property, kind, amount, *, funding=Funding.NONE, account=None, vendor=None, save=True,
           **kw):
    ev = PropertyCostEvent(
        property=property, kind=kind, date=JAN, amount=D(amount), funding_source=funding,
        funding_account=account, vendor_organization=vendor or _org("Vendor"), **kw
    )
    ev.save()
    if save:
        save_cost_event(ev, is_new=True)
    return ev


def _assert_balanced():
    from django.db.models import Sum

    from apps.finance.models import JournalEntry, JournalLine

    agg = JournalLine.objects.filter(entry__status=JournalEntry.Status.POSTED).aggregate(
        d=Sum("base_debit"), c=Sum("base_credit")
    )
    assert agg["d"] == agg["c"], (agg["d"], agg["c"])


# --- capitalization --------------------------------------------------------------------------

def test_owned_cash_purchase_capitalizes_to_node(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        p = _property()
        _event(p, CostKind.PURCHASE, "300000", vendor=_org("Seller"))
        p.refresh_from_db()
        assert p.gl_account is not None and p.gl_account.parent.code == "1410"
        assert account_balance(p.gl_account) == D("300000")
        assert p.cost == D("300000")
        # An accrued (unpaid) purchase adds an asset AND an AP liability → net worth neutral.
        assert net_worth() == ZERO


def test_improvement_and_closing_cost_raise_basis(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        p = _property()
        _event(p, CostKind.PURCHASE, "300000", vendor=_org("Seller"))
        _event(p, CostKind.IMPROVEMENT, "25000")
        _event(p, CostKind.CLOSING_COST, "8000")
        p.refresh_from_db()
        assert account_balance(p.gl_account) == D("333000")


# --- expense routing -------------------------------------------------------------------------

def test_property_tax_posts_to_5810_not_5140(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        p = _property()
        _event(p, CostKind.PROPERTY_TAX, "6000")
        assert account_balance("property_tax_expense") == D("6000")   # 5810 generic
        assert account_balance("property_tax") == ZERO                # 5140 escrow untouched


def test_expense_kinds_route_to_expected_accounts(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        p = _property()
        cases = [
            (CostKind.MAINTENANCE, "5130"),
            (CostKind.HOA, "hoa_fees"),
            (CostKind.UTILITIES, "5120"),
            (CostKind.OTHER, "5900"),
        ]
        for kind, key in cases:
            _event(p, kind, "100")
            assert account_balance(key) == D("100"), key


# --- financed purchase -----------------------------------------------------------------------

def test_financed_purchase_nets_ap_and_holds_mortgage_invariant(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        bank = _bank()
        loan = _mortgage_loan()
        p = _property(OwnershipMode.OWNED_FINANCED)
        ev = PropertyCostEvent(
            property=p, kind=CostKind.PURCHASE, date=JAN, amount=D("400000"),
            vendor_organization=_org("Seller"),
        )
        ev.save()
        settle_financed_purchase(
            ev, down_amount=D("80000"), down_source=Funding.BANK, down_account=bank,
            loan=loan, loan_amount=D("320000"),
        )
        p.refresh_from_db()
        assert account_balance(p.gl_account) == D("400000")     # capitalized in full
        assert account_balance("accounts_payable") == ZERO      # down + mortgage settle the bill
        assert loan.balance == D("320000")                      # mortgage node credited
        assert loan.gl_account.parent.code == "2210"            # under the Mortgage header
        assert p.mortgage_loan_id == loan.pk
        _assert_balanced()


# --- disposal --------------------------------------------------------------------------------

def test_disposal_books_gain_and_derecognizes_node(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        from apps.banking.models import BankTransaction, TxnType

        bank = _bank()
        p = _property()
        _event(p, CostKind.PURCHASE, "300000", vendor=_org("Seller"))
        p.refresh_from_db()
        disp = PropertyDisposal(
            property=p, method=DisposalMethod.SALE, date=MAR, proceeds=D("350000"),
            proceeds_account=bank,
        )
        disp.save()
        post_disposal(disp)
        p.refresh_from_db()
        assert p.is_active is False
        assert account_balance(p.gl_account) == ZERO            # node derecognized
        assert disp.gain_loss == D("50000")                     # 350000 − 300000 gain
        assert account_balance("transfer_clearing") == ZERO     # 1150 nets to zero
        assert BankTransaction.objects.filter(
            account=bank, txn_type=TxnType.TRANSFER_IN, amount=D("350000")
        ).exists()
        _assert_balanced()


def test_disposal_of_mortgaged_property_leaves_mortgage_untouched(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        bank = _bank()
        loan = _mortgage_loan()
        p = _property(OwnershipMode.OWNED_FINANCED)
        ev = PropertyCostEvent(
            property=p, kind=CostKind.PURCHASE, date=JAN, amount=D("400000"),
            vendor_organization=_org("Seller"),
        )
        ev.save()
        settle_financed_purchase(
            ev, down_amount=D("80000"), down_source=Funding.BANK, down_account=bank,
            loan=loan, loan_amount=D("320000"),
        )
        disp = PropertyDisposal(
            property=p, method=DisposalMethod.SALE, date=MAR, proceeds=D("450000"),
            proceeds_account=bank,
        )
        disp.save()
        post_disposal(disp)
        assert loan.balance == D("320000")  # the disposal does NOT pay off the mortgage
        _assert_balanced()


# --- valuation overlay -----------------------------------------------------------------------

def test_valuation_overlay_posts_nothing(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        from apps.finance.models import JournalEntry

        p = _property()
        _event(p, CostKind.PURCHASE, "300000", vendor=_org("Seller"))
        entries_before = JournalEntry.objects.count()
        PropertyValuation.objects.create(property=p, as_of=JAN, value=D("400000"))
        p.refresh_from_db()
        assert p.current_value == D("400000")               # market value overlay
        assert p.cost == D("300000")                        # net worth stays at cost
        assert JournalEntry.objects.count() == entries_before  # nothing posted


# --- value-over-time (appreciation) chart series (Phase 2) -----------------------------------

def test_appreciation_series_carries_value_and_cost(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        from apps.realestate.services import appreciation_series

        p = _property()
        _event(p, CostKind.PURCHASE, "300000", vendor=_org("Seller"))  # cost basis in the GL
        PropertyValuation.objects.create(property=p, as_of=MAR, value=D("350000"))
        p.refresh_from_db()
        data = appreciation_series(p)
        assert data["last_cost"] == D("300000")
        assert data["last_value"] == D("350000")
        assert data["gain"] == D("50000")                   # positive = appreciation above cost
        # The valuation carries forward; the earliest point (before it) falls back to cost.
        assert len(data["series"]) >= 2
        assert data["series"][0][2] == data["series"][0][1]  # pre-valuation: value == cost


def test_appreciation_series_single_point_guard(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        from apps.realestate.services import appreciation_series

        p = _property()  # no purchase, no valuation, no acquisition date
        data = appreciation_series(p)
        assert len(data["series"]) == 1                     # only today's point
        assert data["last_cost"] == data["last_value"] == ZERO


# --- insurance tie-in (covered-property links; Phase 2) --------------------------------------

def _home_policy():
    from apps.insurance.models import InsurancePolicy, PolicyType

    return InsurancePolicy.objects.create(
        policy_type=PolicyType.HOME, insurer_organization=_org("Home Insurer"), currency=_usd()
    )


def test_set_covered_properties_and_read_through(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        from apps.insurance.services import policies_for_asset, set_covered_properties

        p = _property()
        policy = _home_policy()
        set_covered_properties(policy, [p])
        assert list(policies_for_asset(p)) == [policy]
        assert p.active_insurance_policies == [policy]      # model read-through
        set_covered_properties(policy, [])                  # unlink
        assert list(policies_for_asset(p)) == []
        assert p.active_insurance_policies == []


def test_covered_assets_do_not_clobber_across_types(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        from apps.automobile.models import OwnershipMode as VOwn
        from apps.automobile.models import Vehicle
        from apps.insurance.services import set_covered_properties, set_covered_vehicles

        prop = _property()
        vehicle = Vehicle.objects.create(
            nickname="Family SUV", ownership_mode=VOwn.OWNED_CASH, currency=_usd()
        )
        policy = _home_policy()
        set_covered_vehicles(policy, [vehicle])
        set_covered_properties(policy, [prop])
        assert policy.assets.count() == 2                   # both covered
        # Rewriting the vehicle set to empty leaves the property asset untouched (per-CT scope).
        set_covered_vehicles(policy, [])
        assert policy.assets.count() == 1
        assert prop.active_insurance_policies == [policy]
