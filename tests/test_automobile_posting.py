"""Automobile GL/service layer — capitalization, financed purchase, locked bills/payments, the
asset-gate, disposal gain/loss, lease deposit/return, fuel economy, renewals and the value overlay.
"""

import datetime
from decimal import Decimal

import pytest
from django_tenants.utils import schema_context

from apps.automobile.models import (
    ComplianceKind,
    ComplianceResult,
    CostKind,
    DisposalMethod,
    Funding,
    OwnershipMode,
    RegistrationReason,
    ServiceSchedule,
    Vehicle,
    VehicleCostEvent,
    VehicleDisposal,
    VehicleInspection,
    VehiclePropertyTax,
    VehicleRegistration,
    VehicleServiceInvoice,
    VehicleValuation,
)
from apps.automobile.services import (
    cost_by_category,
    delete_cost_event,
    delete_registration,
    delete_service_invoice,
    fuel_economy,
    post_disposal,
    register,
    renewals_due,
    running_cost_total,
    save_cost_event,
    save_inspection,
    save_property_tax,
    save_registration,
    save_service_invoice,
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
            "Insurance", "Registration", "Safety inspection", "Lease ends", "Warranty ends",
        }


# --- registration / inspection / property-tax records (module 8 follow-up) -------------------

def _fee(amount, vendor, funding=Funding.NONE, account=None, due_date=None):
    return {
        "amount": D(amount), "vendor_organization": vendor, "vendor_person": None,
        "funding_source": funding, "funding_account": account, "credit_card": None,
        "cash_account": None, "due_date": due_date, "reference": "", "memo": "",
    }


def test_registration_fee_routes_through_payables_and_updates_caches(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        v = _vehicle()
        bank = _bank_account()
        reg = VehicleRegistration(
            vehicle=v, jurisdiction="Virginia", plate_number="ABC123",
            effective_from=JAN, expires_on=datetime.date(2027, 1, 15),
            reason=RegistrationReason.INITIAL,
        )
        save_registration(reg, fee=_fee("80", _org("DMV"), Funding.BANK, bank))
        v.refresh_from_db()
        assert reg.fee_event is not None
        assert reg.fee_event.bill.is_locked and reg.fee_event.bill.status == "paid"
        assert account_balance("vehicle_registration") == D("80")  # 5350
        assert bank.balance == D("-80")
        assert v.registration_expiry == datetime.date(2027, 1, 15)
        assert v.license_plate == "ABC123" and v.plate_jurisdiction == "Virginia"


def test_registration_history_across_move(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        v = _vehicle()
        reg1 = VehicleRegistration(
            vehicle=v, jurisdiction="New York", plate_number="NY111",
            effective_from=datetime.date(2024, 1, 1), expires_on=datetime.date(2025, 1, 1),
            reason=RegistrationReason.INITIAL,
        )
        save_registration(reg1)
        reg2 = VehicleRegistration(
            vehicle=v, jurisdiction="Virginia", plate_number="VA222",
            effective_from=datetime.date(2025, 6, 1), expires_on=datetime.date(2026, 6, 1),
            reason=RegistrationReason.MOVED,
        )
        save_registration(reg2)
        v.refresh_from_db()
        assert v.registrations.count() == 2               # history intact
        assert v.current_plate == "VA222"                 # current = latest ≤ today
        assert v.current_plate_state == "Virginia"
        assert v.license_plate == "VA222"                 # cache follows the move
        assert reg2.is_current and not reg1.is_current


def test_registration_record_only_no_fee(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        from apps.payables.models import Bill

        v = _vehicle()
        reg = VehicleRegistration(
            vehicle=v, jurisdiction="Texas", plate_number="TX1", effective_from=JAN,
            expires_on=datetime.date(2027, 1, 15),
        )
        save_registration(reg)  # no fee dict
        assert reg.fee_event is None
        assert Bill.objects.count() == 0
        v.refresh_from_db()
        assert v.registration_expiry == datetime.date(2027, 1, 15)


def test_financed_registration_defaults_lien_and_lienholder(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        from apps.automobile.models import TitleStatus

        loan = _auto_loan()
        v = _vehicle(ownership_mode=OwnershipMode.OWNED_FINANCED, loan=loan)
        reg = VehicleRegistration(
            vehicle=v, jurisdiction="Ohio", plate_number="OH9", effective_from=JAN,
        )
        save_registration(reg)
        reg.refresh_from_db()
        assert reg.title_status == TitleStatus.LIEN
        assert reg.lienholder_organization_id == loan.lender_organization_id


def test_delete_registration_erases_fee_bill_and_refuses_foreign_payment(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        from apps.payables.models import Bill, Payment
        from apps.payables.services import apply_payment

        v = _vehicle()
        dmv = _org("DMV")
        reg = VehicleRegistration(
            vehicle=v, jurisdiction="Virginia", plate_number="ABC123", effective_from=JAN,
        )
        # Unfunded fee → an accrued (open) bill; then a FOREIGN payables payment settles it.
        save_registration(reg, fee=_fee("80", dmv, Funding.NONE))
        bill_id = reg.fee_event.bill_id
        foreign = Payment(
            vendor_organization=dmv, date=JAN, amount=D("80"),
            funding_kind=Payment.Funding.CASH,
        )
        foreign.save()
        apply_payment(foreign, [(reg.fee_event.bill, D("80"))])
        with pytest.raises(ValueError):
            delete_registration(reg)
        # The atomic block rolled back — the record + fee bill survive.
        reg = VehicleRegistration.objects.get(pk=reg.pk)
        assert reg.fee_event_id is not None
        assert Bill.all_objects.filter(pk=bill_id).exists()
        # After removing the foreign payment, the delete succeeds and erases the fee bill.
        foreign.allocations.all().delete()
        if foreign.journal_entry_id:
            foreign.journal_entry.hard_delete()
        foreign.hard_delete()
        delete_registration(reg)
        assert not Bill.all_objects.filter(pk=bill_id).exists()
        assert not VehicleRegistration.objects.filter(pk=reg.pk).exists()


def test_safety_inspection_advances_due_and_renewals(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        v = _vehicle()
        soon = datetime.date.today() + datetime.timedelta(days=20)
        insp = VehicleInspection(
            vehicle=v, kind=ComplianceKind.SAFETY, performed_on=JAN,
            result=ComplianceResult.PASS, expires_on=soon,
        )
        save_inspection(insp)
        v.refresh_from_db()
        assert v.inspection_due == soon
        labels = {r["label"] for r in renewals_due(45) if r["vehicle"].pk == v.pk}
        assert "Safety inspection" in labels


def test_emissions_fee_folds_to_5350_and_is_biennial(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        v = _vehicle()
        bank = _bank_account()
        insp = VehicleInspection(
            vehicle=v, kind=ComplianceKind.EMISSIONS, performed_on=JAN,
            result=ComplianceResult.PASS, expires_on=datetime.date(2028, 1, 15),
        )
        save_inspection(insp, fee=_fee("30", _org("Smog Shop"), Funding.BANK, bank))
        assert account_balance("vehicle_registration") == D("30")  # emissions folds into 5350
        assert insp.fee_event.kind == CostKind.EMISSIONS
        v.refresh_from_db()
        assert v.emissions_due == datetime.date(2028, 1, 15)
        assert v.inspection_due is None  # emissions does not satisfy the safety due date


def test_combined_inspection_advances_both_due_dates(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        v = _vehicle()
        due = datetime.date(2027, 1, 15)
        insp = VehicleInspection(
            vehicle=v, kind=ComplianceKind.COMBINED, performed_on=JAN,
            result=ComplianceResult.PASS, expires_on=due,
        )
        save_inspection(insp)
        v.refresh_from_db()
        assert v.inspection_due == due and v.emissions_due == due  # one sticker, both due dates


def test_not_required_inspection_no_nag(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        v = _vehicle()
        insp = VehicleInspection(
            vehicle=v, kind=ComplianceKind.SAFETY, performed_on=JAN,
            result=ComplianceResult.NOT_REQUIRED, expires_on=None,
        )
        save_inspection(insp)
        v.refresh_from_db()
        assert v.inspection_due is None
        assert insp.is_exempt


def test_exempt_toggle_suppresses_renewal_row(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        soon = datetime.date.today() + datetime.timedelta(days=20)
        v = _vehicle(
            inspection_exempt=True, emissions_exempt=True,
            inspection_due=soon, emissions_due=soon,
        )
        labels = {r["label"] for r in renewals_due(45) if r["vehicle"].pk == v.pk}
        assert "Safety inspection" not in labels and "Emissions test" not in labels


def _fee_pt(pt, vendor, funding=Funding.NONE, account=None):
    return {
        "amount": pt.amount, "vendor_organization": vendor, "vendor_person": None,
        "funding_source": funding, "funding_account": account, "credit_card": None,
        "cash_account": None, "due_date": pt.due_date, "reference": "", "memo": "",
    }


def test_property_tax_accrued_posts_to_5810_and_sets_reminder(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        v = _vehicle()
        pt = VehiclePropertyTax(
            vehicle=v, tax_year=2026, jurisdiction="Fairfax County", amount=D("450"),
            due_date=datetime.date(2026, 9, 5),
        )
        save_property_tax(pt, fee=_fee_pt(pt, _org("Fairfax County")))
        assert account_balance("property_tax_expense") == D("450")  # 5810
        assert account_balance("accounts_payable") == D("450")      # accrued (unpaid)
        v.refresh_from_db()
        assert v.property_tax_due == datetime.date(2026, 9, 5)
        labels = {r["label"] for r in renewals_due(120) if r["vehicle"].pk == v.pk}
        assert "Property tax" in labels


def test_property_tax_funded_clears_reminder(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        v = _vehicle()
        bank = _bank_account()
        pt = VehiclePropertyTax(
            vehicle=v, tax_year=2026, jurisdiction="Fairfax County", amount=D("450"),
            due_date=datetime.date(2026, 9, 5),
        )
        save_property_tax(pt, fee=_fee_pt(pt, _org("Fairfax County"), Funding.BANK, bank))
        assert pt.fee_event.bill.status == "paid"
        assert account_balance("accounts_payable") == ZERO
        assert account_balance("property_tax_expense") == D("450")
        assert bank.balance == D("-450")
        v.refresh_from_db()
        assert v.property_tax_due is None  # paid → reminder cleared


# --- multi-line service invoices -------------------------------------------------------------

def test_service_invoice_builds_category_bill_summing_to_grand_total(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        v = _vehicle()
        inv = VehicleServiceInvoice(
            vehicle=v, date=JAN, vendor_organization=_org("Priority Nissan"),
            invoice_number="325554", sublet=D("50"), shop_supplies=D("10"),
            discount=D("20"), sales_tax=D("15"),
        )
        jobs = [
            {"code": "PFL", "complaint": "oil life low", "labor_amount": D("60"),
             "parts": [{"part_number": "OIL-5W30", "description": "oil",
                        "quantity": D("5"), "unit_price": D("8")}]},
            {"code": "BRK", "labor_amount": D("120"),
             "parts": [{"part_number": "PAD", "quantity": D("1"), "unit_price": D("90")}]},
        ]
        save_service_invoice(inv, jobs)
        # parts = 5*8 + 90 = 130; labor = 60 + 120 = 180; grand = 180+130+50+10+15-20 = 365
        assert inv.parts_total == D("130")
        assert inv.labor_total == D("180")
        assert inv.grand_total == D("365")
        assert inv.bill is not None and inv.bill.is_locked
        assert inv.bill.total == D("365")                    # category lines sum to grand total
        assert account_balance("5320") == D("370")           # parts+labor+sublet+shop supplies
        assert account_balance("sales_tax_paid") == D("15")  # sales tax → the sales-tax account
        assert account_balance("accounts_payable") == D("365")  # unpaid


def test_service_invoice_funding_creates_locked_payment(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        v = _vehicle()
        bank = _bank_account()
        inv = VehicleServiceInvoice(
            vehicle=v, date=JAN, vendor_organization=_org("Shop"),
            funding_source=Funding.BANK, funding_account=bank,
        )
        save_service_invoice(inv, [{"code": "X", "labor_amount": D("100"), "parts": []}])
        assert inv.grand_total == D("100")
        assert inv.bill.status == "paid"
        assert inv.payment is not None and inv.payment.is_locked
        assert bank.balance == D("-100")


def test_service_invoice_advances_schedule_odometer_and_register(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        v = _vehicle()
        sched = ServiceSchedule.objects.create(
            vehicle=v, name="Oil change", interval_months=6, interval_miles=5000
        )
        inv = VehicleServiceInvoice(
            vehicle=v, date=JAN, vendor_organization=_org("Shop"), odometer_out=12000
        )
        save_service_invoice(inv, [{"code": "PFL", "labor_amount": D("80"), "parts": []}])
        sched.refresh_from_db()
        assert sched.last_done_date == JAN
        assert sched.next_due_mileage == 17000
        v.refresh_from_db()
        assert v.current_mileage == 12000                    # odometer upserted
        rows = register(v)
        assert any(getattr(r, "is_service_invoice", False) for r in rows)  # merged register
        assert running_cost_total(v) == D("80")              # counted once in TCO
        _segs, total = cost_by_category(v)
        assert total == D("80")


def test_zero_service_invoice_creates_no_bill(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        from apps.payables.models import Bill

        v = _vehicle()
        inv = VehicleServiceInvoice(vehicle=v, date=JAN, vendor_organization=_org("Shop"))
        save_service_invoice(inv, [{"code": "WARRANTY", "labor_amount": D("0"), "parts": []}])
        assert inv.grand_total == D("0")
        assert inv.bill is None
        assert Bill.objects.count() == 0
        assert inv.jobs.count() == 1  # history recorded


def test_delete_service_invoice_unwinds_bill_and_payment(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        from apps.payables.models import Bill, Payment

        v = _vehicle()
        bank = _bank_account()
        inv = VehicleServiceInvoice(
            vehicle=v, date=JAN, vendor_organization=_org("Shop"),
            funding_source=Funding.BANK, funding_account=bank,
        )
        save_service_invoice(inv, [{"code": "X", "labor_amount": D("100"), "parts": []}])
        bill_id, pay_id = inv.bill_id, inv.payment_id
        delete_service_invoice(inv)
        assert not VehicleServiceInvoice.all_objects.filter(pk=inv.pk).exists()
        assert not Bill.all_objects.filter(pk=bill_id).exists()
        assert not Payment.all_objects.filter(pk=pay_id).exists()
        assert bank.balance == ZERO
