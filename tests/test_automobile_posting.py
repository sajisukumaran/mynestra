"""Automobile GL/service layer — capitalization, financed purchase, locked bills/payments, the
asset-gate, disposal gain/loss, lease deposit/return, fuel economy, renewals and the value overlay.
"""

import datetime
from decimal import Decimal

from django_tenants.utils import schema_context

from apps.automobile.models import (
    CostKind,
    DisposalMethod,
    Funding,
    OwnershipMode,
    Vehicle,
    VehicleCostEvent,
    VehicleDisposal,
    VehicleValuation,
)
from apps.automobile.services import (
    delete_cost_event,
    fuel_economy,
    post_disposal,
    renewals_due,
    save_cost_event,
    settle_financed_purchase,
)
from apps.finance.models import JournalEntry
from apps.finance.services import account_balance, net_worth

D = Decimal
ZERO = D("0")
JAN = datetime.date(2026, 1, 15)


def _org(name="Dealer"):
    from apps.organizations.models import Organization

    return Organization.objects.create(name=name)


def _usd():
    from apps.finance.models import Currency

    return Currency.objects.get(code="USD")


def _vehicle(**kw):
    defaults = {
        "nickname": "Family SUV", "ownership_mode": OwnershipMode.OWNED_CASH, "currency": _usd(),
    }
    defaults.update(kw)
    return Vehicle.objects.create(**defaults)


def _bank_account(nickname="Checking"):
    from apps.banking.models import AccountType as BAT
    from apps.banking.models import BankAccount
    from apps.banking.services import ensure_gl_account as bank_gl

    acct = BankAccount.objects.create(
        bank=_org("My Bank"), account_type=BAT.CHECKING, nickname=nickname, currency=_usd()
    )
    bank_gl(acct)
    return acct


def _auto_loan(nickname="Auto loan"):
    from apps.loans.models import Loan, LoanType

    return Loan.objects.create(
        loan_type=LoanType.AUTO, nickname=nickname, currency=_usd(),
        lender_organization=_org("Auto Finance Co"),
    )


def _event(vehicle, kind, amount, *, save=True, **kw):
    kw.setdefault("vendor_organization", _org("Vendor"))
    ev = VehicleCostEvent(vehicle=vehicle, kind=kind, date=kw.pop("date", JAN), amount=amount, **kw)
    ev.save()
    if save:
        save_cost_event(ev, is_new=True)
    return ev


# --- capitalization + net worth --------------------------------------------------------------

def test_owned_cash_purchase_capitalizes_to_vehicle_node(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        v = _vehicle()
        ev = _event(v, CostKind.PURCHASE, D("30000"), vendor_organization=_org("Dealer"))
        v.refresh_from_db()
        assert v.gl_account is not None and v.gl_account.parent.code == "1420"
        assert account_balance(v.gl_account) == D("30000")  # Dr 1420.NN
        assert v.cost == D("30000")
        assert account_balance("accounts_payable") == D("30000")  # unpaid → AP outstanding
        assert ev.bill.is_locked and ev.bill.status == "open"


def test_purchase_plus_improvement_raises_basis(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        v = _vehicle()
        _event(v, CostKind.PURCHASE, D("30000"))
        _event(v, CostKind.IMPROVEMENT, D("2500"))
        v.refresh_from_db()
        assert account_balance(v.gl_account) == D("32500")  # cost grows with improvements
        assert v.cost == D("32500")


def test_funded_purchase_settles_ap_and_moves_net_worth_neutrally(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        v = _vehicle()
        bank = _bank_account()
        ev = _event(
            v, CostKind.PURCHASE, D("30000"), vendor_organization=_org("Dealer"),
            funding_source=Funding.BANK, funding_account=bank,
        )
        assert account_balance("accounts_payable") == ZERO  # settled by the payment
        assert bank.balance == D("-30000")
        assert ev.bill.status == "paid"
        assert ev.payment is not None and ev.payment.is_locked
        assert net_worth() == ZERO  # −30000 cash + 30000 vehicle


def test_no_asset_item_for_automobile_sourced_bill(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        from apps.payables.models import AssetItem

        v = _vehicle()
        ev = _event(v, CostKind.PURCHASE, D("30000"))
        assert not AssetItem.objects.filter(bill_line__bill=ev.bill).exists()  # F5 gate


# --- financed purchase (loan-funded settlement) ----------------------------------------------

def test_financed_purchase_nets_ap_and_holds_loan_invariant(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        v = _vehicle(ownership_mode=OwnershipMode.OWNED_FINANCED)
        bank = _bank_account()
        loan = _auto_loan()
        ev = VehicleCostEvent(
            vehicle=v, kind=CostKind.PURCHASE, date=JAN, amount=D("30000"),
            vendor_organization=_org("Dealer"),
        )
        ev.save()
        settle_financed_purchase(
            ev, down_amount=D("5000"), down_source=Funding.BANK, down_account=bank,
            loan=loan, loan_amount=D("25000"),
        )
        v.refresh_from_db()
        assert account_balance(v.gl_account) == D("30000")   # 1420.NN
        assert loan.balance == D("25000")                    # 2220.NN
        assert loan.gl_account.parent.code == "2220"
        assert account_balance("accounts_payable") == ZERO   # bill fully settled
        assert bank.balance == D("-5000")
        assert v.loan_id == loan.pk
        ev.bill.refresh_from_db()
        assert ev.bill.status == "paid"
        # loan invariant: balance owed == Σ txn balance_delta
        delta = sum((t.balance_delta for t in loan.transactions.all()), ZERO)
        assert account_balance(loan.gl_account) == delta
        # Net-worth neutral: 30000 vehicle − 5000 cash − 25000 debt = 0.
        assert net_worth() == ZERO


# --- running costs (locked bill + optional locked payment) -----------------------------------

def test_funded_fuel_event_expenses_and_locks(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        v = _vehicle()
        bank = _bank_account()
        ev = _event(
            v, CostKind.FUEL, D("60"), vendor_organization=_org("Shell"),
            funding_source=Funding.BANK, funding_account=bank,
            fuel_volume=D("12"), fuel_unit="gal", odometer=10000, is_full_tank=True,
        )
        assert account_balance("5310") == D("60")  # fuel expense
        assert ev.bill.status == "paid"
        assert ev.bill.is_locked and ev.payment.is_locked
        assert bank.balance == D("-60")
        v.refresh_from_db()
        assert v.current_mileage == 10000  # odometer denorm refreshed


def test_delete_funded_cost_event_erases_bill_and_payment(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        from apps.payables.models import Bill, Payment

        v = _vehicle()
        bank = _bank_account()
        ev = _event(
            v, CostKind.SERVICE, D("400"), funding_source=Funding.BANK, funding_account=bank,
        )
        bill_id, pay_id = ev.bill_id, ev.payment_id
        assert bank.balance == D("-400")
        delete_cost_event(ev)
        assert not VehicleCostEvent.all_objects.filter(pk=ev.pk).exists()
        assert not Bill.all_objects.filter(pk=bill_id).exists()
        assert not Payment.all_objects.filter(pk=pay_id).exists()
        assert bank.balance == ZERO  # funding leg unwound
        assert account_balance("5320") == ZERO


# --- disposal --------------------------------------------------------------------------------

def _buy_for(v, amount, bank):
    return _event(
        v, CostKind.PURCHASE, amount, vendor_organization=_org("Dealer"),
        funding_source=Funding.BANK, funding_account=bank,
    )


def test_disposal_sale_at_loss_books_gain_loss_and_bank_leg(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        v = _vehicle()
        _buy_for(v, D("30000"), _bank_account())
        proceeds_bank = _bank_account("Proceeds")
        disp = VehicleDisposal(
            vehicle=v, method=DisposalMethod.SALE, date=datetime.date(2026, 6, 1),
            proceeds=D("18000"), proceeds_account=proceeds_bank,
        )
        disp.save()
        post_disposal(disp)
        assert account_balance(v.gl_account) == ZERO           # node derecognized (30000−30000)
        assert account_balance("asset_disposal_gain_loss") == D("-12000")  # a loss (rev debit)
        assert disp.bank_txn_id is not None
        assert proceeds_bank.balance == D("18000")             # proceeds landed via 1150 leg
        assert account_balance("1150") == ZERO                 # clearing nets
        assert disp.gain_loss == D("-12000")
        v.refresh_from_db()
        assert v.is_active is False and v.is_disposed


def test_disposal_sale_at_gain(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        v = _vehicle()
        _buy_for(v, D("30000"), _bank_account())
        proceeds_bank = _bank_account("Proceeds")
        disp = VehicleDisposal(
            vehicle=v, method=DisposalMethod.SALE, date=datetime.date(2026, 6, 1),
            proceeds=D("35000"), proceeds_account=proceeds_bank,
        )
        disp.save()
        post_disposal(disp)
        assert account_balance("asset_disposal_gain_loss") == D("5000")  # a gain (rev credit)
        assert proceeds_bank.balance == D("35000")


def test_trade_in_clears_1150_and_part_pays_new_bill(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        old = _vehicle(nickname="Old car")
        _buy_for(old, D("30000"), _bank_account())
        new = _vehicle(nickname="New car")
        new_ev = _event(
            new, CostKind.PURCHASE, D("40000"), vendor_organization=_org("Dealer"),
        )  # unfunded → OPEN bill
        disp = VehicleDisposal(
            vehicle=old, method=DisposalMethod.TRADE_IN, date=datetime.date(2026, 6, 1),
            proceeds=D("18000"),
        )
        disp.save()
        post_disposal(disp, trade_bill=new_ev.bill)
        assert account_balance("1150") == ZERO           # Dr 1150 18000 (disposal) − Cr (payment)
        new_ev.bill.refresh_from_db()
        assert new_ev.bill.balance_due == D("22000")     # 40000 − 18000 trade allowance
        assert disp.trade_payment_id is not None


# --- lease ------------------------------------------------------------------------------------

def test_lease_deposit_capitalizes_to_1320(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        v = _vehicle(ownership_mode=OwnershipMode.LEASED, lease_security_deposit=D("2000"))
        bank = _bank_account()
        _event(
            v, CostKind.LEASE_DEPOSIT, D("2000"), vendor_organization=_org("Lessor"),
            funding_source=Funding.BANK, funding_account=bank,
        )
        assert account_balance("refundable_deposits") == D("2000")  # 1320 asset
        assert bank.balance == D("-2000")


def test_lease_return_refunds_and_withholds(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        v = _vehicle(ownership_mode=OwnershipMode.LEASED, lease_security_deposit=D("2000"))
        bank = _bank_account()
        _event(
            v, CostKind.LEASE_DEPOSIT, D("2000"), vendor_organization=_org("Lessor"),
            funding_source=Funding.BANK, funding_account=bank,
        )
        refund_bank = _bank_account("Refund")
        disp = VehicleDisposal(
            vehicle=v, method=DisposalMethod.LEASE_RETURN, date=datetime.date(2026, 6, 1),
            proceeds=D("1500"), proceeds_account=refund_bank,  # 500 withheld
        )
        disp.save()
        post_disposal(disp)
        assert account_balance("refundable_deposits") == ZERO   # deposit derecognized
        assert account_balance("vehicle_lease") == D("500")     # withheld → 5360 lease expense
        assert refund_bank.balance == D("1500")
        assert account_balance("1150") == ZERO


# --- overlays / read models ------------------------------------------------------------------

def test_valuation_overlay_posts_nothing_networth_stays_at_cost(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        v = _vehicle()
        _buy_for(v, D("30000"), _bank_account())
        before = JournalEntry.objects.count()
        VehicleValuation.objects.create(
            vehicle=v, as_of=datetime.date(2026, 6, 1), value=D("25000")
        )
        assert JournalEntry.objects.count() == before  # pure overlay
        v.refresh_from_db()
        assert v.current_value == D("25000")
        assert v.cost == D("30000")          # net worth stays at cost
        assert v.depreciation == D("5000")


def test_fuel_economy_across_partial_fills(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        v = _vehicle()
        _event(v, CostKind.FUEL, D("40"), fuel_volume=D("8"), fuel_unit="gal",
               odometer=1000, is_full_tank=True, date=datetime.date(2026, 1, 1))
        _event(v, CostKind.FUEL, D("25"), fuel_volume=D("5"), fuel_unit="gal",
               odometer=1150, is_full_tank=False, date=datetime.date(2026, 1, 10))
        _event(v, CostKind.FUEL, D("50"), fuel_volume=D("10"), fuel_unit="gal",
               odometer=1300, is_full_tank=True, date=datetime.date(2026, 1, 20))
        econ = fuel_economy(v)
        assert len(econ) == 1
        # distance between full fills 1300−1000 = 300; volume 5 + 10 = 15 → 20 mi/gal
        assert econ[0]["economy"] == D("20.0")
        assert econ[0]["label"] == "mi/gal"


def test_renewals_due_covers_all_five_date_kinds(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        soon = datetime.date.today() + datetime.timedelta(days=20)
        v = _vehicle(
            insurance_expiry=soon, registration_expiry=soon, inspection_due=soon,
            lease_end_date=soon, warranty_expiry=soon,
        )
        rows = [r for r in renewals_due(45) if r["vehicle"].pk == v.pk]
        assert {r["label"] for r in rows} == {
            "Insurance", "Registration", "Inspection", "Lease ends", "Warranty ends",
        }
