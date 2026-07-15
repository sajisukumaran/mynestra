"""Automobile service layer — the only sanctioned GL path for the Vehicles module.

Two GL-touching flows, both through existing service layers (never hand-written ledger rows):

1. **Cost events → locked Payables bills (+ optional locked Payments).** A `VehicleCostEvent`
   materializes a `payables.Bill` (`is_locked=True`, `source=<event>`, one line to the right GL
   account) via `payables.services.post_bill`; when funded, a locked `payables.Payment`
   (BANK/CARD/CASH) allocated to the bill via `apply_payment`. A financed purchase settles the
   dealer bill with a bank down-payment + a `Payment.Funding.LOAN` disbursement.
2. **Disposal → a direct finance entry** (`VehicleDisposal`) booking proceeds vs cost with the
   difference to `4930`. Proceeds to a tracked bank route via `1150` + a native banking TRANSFER_IN.

An **owned** vehicle owns one postable ASSET node under `1420 Vehicles` (`ensure_gl_account`), so
its cost is `account_balance(gl_account)`. A **leased** vehicle has no node. The value-over-time
overlay (`VehicleValuation`) posts nothing.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from django.db import transaction

from apps.automobile.models import (
    CAPITALIZING_KINDS,
    DEPOSIT_KINDS,
    RENEWAL_KINDS,
    SERVICE_KINDS,
    CostKind,
    DisposalMethod,
    Funding,
    OdometerReading,
    Vehicle,
    VehicleCostEvent,
    VehicleDisposal,
)
from apps.finance.models import ZERO, Account, AccountType, JournalEntry, Side
from apps.finance.services import (
    LineInput,
    account_balance,
    post_entry,
    resolve_account,
    resolve_posting_account,
    reverse_entry,
)

# Fixed accounts (resolved by stable system_key / code). Structural legs are never remappable.
VEHICLES_HEADER = "1420"                       # Vehicles (per-vehicle nodes nest under it)
REFUNDABLE_DEPOSITS = "refundable_deposits"    # 1320
DISPOSAL_GAIN_LOSS = "asset_disposal_gain_loss"  # 4930
TRANSFER_CLEARING = "transfer_clearing"        # 1150
CASH_ON_HAND = "1110"
ACCOUNTS_PAYABLE = "accounts_payable"          # 2300

# Category activities the Expert-mode Accounting tab can remap, per vehicle. Structural legs (the
# asset node 1420.NN, the 1320 deposit) are never here — they're capitalizing, not expenses.
POSTING_ACTIVITIES = [
    {"key": "fuel", "label": "Fuel / charging", "kind": "expense", "default": "5310"},
    {"key": "service", "label": "Service & repairs", "kind": "expense", "default": "5320"},
    {"key": "insurance", "label": "Insurance", "kind": "expense", "default": "vehicle_insurance"},
    {"key": "registration", "label": "Registration & inspection", "kind": "expense",
     "default": "vehicle_registration"},
    {"key": "lease", "label": "Lease payments", "kind": "expense", "default": "vehicle_lease"},
    {"key": "tax_fee", "label": "Taxes & fees", "kind": "expense", "default": "5800"},
]

# CostKind → (Expert activity key, Standard default account). An annual excise/property tax_fee
# defaults to 5800 Taxes (5930 Sales Tax is the payables purchases default — the wrong home here).
KIND_ACTIVITY = {
    CostKind.FUEL: ("fuel", "5310"),
    CostKind.SERVICE: ("service", "5320"),
    CostKind.REPAIR: ("service", "5320"),
    CostKind.INSURANCE: ("insurance", "vehicle_insurance"),
    CostKind.REGISTRATION: ("registration", "vehicle_registration"),
    CostKind.INSPECTION: ("registration", "vehicle_registration"),
    CostKind.LEASE_PAYMENT: ("lease", "vehicle_lease"),
    CostKind.TAX_FEE: ("tax_fee", "5800"),
    CostKind.OTHER: (None, "5900"),
}
# Running-cost kinds counted in TCO / cost-per-mile (not capitalizing, not a recoverable deposit).
RUNNING_COST_KINDS = frozenset(
    {CostKind.FUEL, CostKind.SERVICE, CostKind.REPAIR, CostKind.INSURANCE,
     CostKind.REGISTRATION, CostKind.INSPECTION, CostKind.LEASE_PAYMENT, CostKind.TAX_FEE,
     CostKind.OTHER}
)


# --- GL account provisioning (owned vehicles only) -------------------------------------------

def _gl_name(vehicle: Vehicle) -> str:
    return vehicle.nickname


def _next_child_code(parent: Account) -> str:
    """The next free `<parent.code>.NN` code."""
    prefix = f"{parent.code}."
    highest = 0
    for code in Account.objects.filter(parent=parent).values_list("code", flat=True):
        if code.startswith(prefix):
            suffix = code[len(prefix):]
            if suffix.isdigit():
                highest = max(highest, int(suffix))
    return f"{parent.code}.{highest + 1:02d}"


def ensure_gl_account(vehicle: Vehicle, *, parent=None, existing=None) -> Account:
    """Create (or reconcile) the postable ASSET node carrying this owned vehicle's cost, nested
    under `1420 Vehicles`. After disposal the node nets to 0 but is kept (it has posted lines)."""
    if vehicle.gl_account_id:
        gl = vehicle.gl_account
        changed = []
        name = _gl_name(vehicle)
        if gl.name != name:
            gl.name = name
            changed.append("name")
        if gl.currency_id != vehicle.currency_id and not gl.lines.exists():
            gl.currency = vehicle.currency
            changed.append("currency")
        if changed:
            gl.save(update_fields=[*changed, "updated_at"])
        return gl

    if existing is not None:
        vehicle.gl_account = existing
        vehicle.save(update_fields=["gl_account"])
        return existing

    parent = parent or resolve_account(VEHICLES_HEADER)
    gl = Account.objects.create(
        code=_next_child_code(parent),
        name=_gl_name(vehicle),
        type=AccountType.ASSET,
        normal_side=Side.DEBIT,
        currency=vehicle.currency,
        parent=parent,
        is_postable=True,
        is_system=False,
    )
    vehicle.gl_account = gl
    vehicle.save(update_fields=["gl_account"])
    return gl


def _bill_line_account(event: VehicleCostEvent) -> Account:
    """The GL account a cost event's single bill line posts to. Capitalizing kinds are structural
    (the vehicle node / 1320) and never remappable; expense kinds honor the per-vehicle posting
    map in Expert mode."""
    vehicle = event.vehicle
    if event.kind in CAPITALIZING_KINDS:  # purchase / improvement → the vehicle's own node
        return ensure_gl_account(vehicle)
    if event.kind in DEPOSIT_KINDS:  # lease deposit → the shared refundable-deposit asset
        return resolve_account(REFUNDABLE_DEPOSITS)
    key, default = KIND_ACTIVITY.get(event.kind, (None, "5900"))
    if key is None:
        return resolve_account(default)
    return resolve_posting_account(vehicle, key, default)


# --- Vendor tagging (reuse the Payables catalog) ---------------------------------------------

def _ensure_vendor_category(org) -> None:
    from apps.setup.models import Category

    cat = Category.objects.filter(kind=Category.Kind.ORG, name="Vendor").first()
    if cat:
        org.categories.add(cat)


def _ensure_vendor_profile(event: VehicleCostEvent) -> None:
    from apps.payables.services import ensure_vendor_profile

    if event.vendor_organization_id:
        _ensure_vendor_category(event.vendor_organization)
        ensure_vendor_profile(organization=event.vendor_organization)
    elif event.vendor_person_id:
        ensure_vendor_profile(person=event.vendor_person)


# --- Locked bill + payment sync --------------------------------------------------------------

def _event_ct():
    from django.contrib.contenttypes.models import ContentType

    return ContentType.objects.get_for_model(VehicleCostEvent)


def _bill_line_desc(event: VehicleCostEvent) -> str:
    vehicle = event.vehicle
    if event.kind == CostKind.PURCHASE:
        return f"{vehicle.full_name} — purchase"
    if event.memo:
        return event.memo
    return f"{vehicle.nickname} — {event.get_kind_display().lower()}"


def _sync_bill(event: VehicleCostEvent, *, user=None):
    """Create (or repost in place) the locked Payables bill backing this cost event — one line to
    the right GL account, tagged as sourced from this event."""
    from apps.payables.models import Bill, BillLine
    from apps.payables.services import post_bill, repost_bill

    vehicle = event.vehicle
    bill = event.bill or Bill(is_locked=True)
    bill.vendor_person = event.vendor_person
    bill.vendor_organization = event.vendor_organization
    bill.bill_date = event.date
    bill.due_date = event.due_date or event.date
    bill.currency = vehicle.currency
    bill.vendor_ref = event.reference
    bill.notes = event.memo
    bill.is_locked = True
    bill.source_content_type = _event_ct()
    bill.source_object_id = event.pk
    bill.save()

    bill.lines.all().delete()  # single-line, rewritten each save
    account = _bill_line_account(event)
    quantity = Decimal("1")
    unit_price = event.amount
    # Fuel/charging: carry the volume so the payables line renders truthfully (qty × unit price).
    if event.kind == CostKind.FUEL and event.fuel_volume and event.fuel_volume > ZERO:
        quantity = event.fuel_volume
        unit_price = (event.amount / event.fuel_volume)
    BillLine.objects.create(
        bill=bill, line_type=BillLine.LineType.EXPENSE, order=0,
        description=_bill_line_desc(event), account=account,
        quantity=quantity, unit_price=unit_price,
    )

    if event.bill_id is None:
        post_bill(bill, user=user)
        event.bill = bill
        event.save(update_fields=["bill", "updated_at"])
    else:
        repost_bill(bill, user=user)
    return bill


def _payment_funding(source: str):
    from apps.payables.models import Payment

    return {
        Funding.BANK: Payment.Funding.BANK,
        Funding.CARD: Payment.Funding.CARD,
        Funding.CASH: Payment.Funding.CASH,
    }[source]


def _module_payments(event: VehicleCostEvent):
    """Every locked Payables payment sourced to this event (a funded cost event has one; a financed
    purchase has a down payment + a loan payment)."""
    from apps.payables.models import Payment

    return Payment.objects.filter(source_content_type=_event_ct(), source_object_id=event.pk)


def _teardown_module_payments(event: VehicleCostEvent, *, user=None):
    from apps.payables.services import delete_payment

    for pay in list(_module_payments(event)):
        delete_payment(pay, user=user)
        pay.hard_delete()
    if event.payment_id is not None:
        event.payment = None
        event.save(update_fields=["payment", "updated_at"])


def _new_locked_payment(event: VehicleCostEvent):
    from apps.payables.models import Payment

    return Payment(
        vendor_person=event.vendor_person,
        vendor_organization=event.vendor_organization,
        date=event.date,
        is_locked=True,
        source_content_type=_event_ct(),
        source_object_id=event.pk,
    )


def _sync_single_payment(event: VehicleCostEvent, *, user=None):
    """Create / repost / remove the ONE locked funding payment for a plain cost event, allocated in
    full to its bill. NONE funding leaves the bill accrued (unpaid)."""
    from apps.payables.services import apply_payment, repost_payment

    bill = event.bill
    if not event.is_funded:
        _teardown_module_payments(event, user=user)
        return None

    pay = event.payment or _new_locked_payment(event)
    pay.vendor_person = event.vendor_person
    pay.vendor_organization = event.vendor_organization
    pay.date = event.date
    pay.amount = event.amount
    pay.funding_kind = _payment_funding(event.funding_source)
    pay.bank_account = event.funding_account if event.funding_source == Funding.BANK else None
    pay.credit_card = event.credit_card if event.funding_source == Funding.CARD else None
    pay.cash_account = event.cash_account if event.funding_source == Funding.CASH else None
    pay.is_locked = True
    pay.source_content_type = _event_ct()
    pay.source_object_id = event.pk

    if event.payment_id is None:
        pay.save()
        apply_payment(pay, [(bill, event.amount)], user=user)
        event.payment = pay
        event.save(update_fields=["payment", "updated_at"])
    else:
        pay.save()
        repost_payment(pay, [(bill, event.amount)], user=user)
    return pay


# --- Cost-event orchestration ----------------------------------------------------------------

@transaction.atomic
def save_cost_event(event: VehicleCostEvent, *, user=None, is_new=True):
    """Post a saved cost event: ensure the GL node (capitalizing + owned), tag the vendor, build the
    locked bill and (if funded) the locked payment, then refresh mileage / service / renewals. The
    caller has already resolved + set the vendor party and saved the event row (so it has a pk)."""
    vehicle = event.vehicle
    if event.kind in CAPITALIZING_KINDS and vehicle.is_owned:
        ensure_gl_account(vehicle)
    _ensure_vendor_profile(event)
    _sync_bill(event, user=user)
    _sync_single_payment(event, user=user)
    _apply_event_side_effects(event)
    return event


def _apply_event_side_effects(event: VehicleCostEvent):
    """Odometer upsert, service-schedule advance, vehicle denorm refresh (mileage + renewals)."""
    vehicle = event.vehicle
    if event.odometer is not None:
        source = (
            OdometerReading.Source.PURCHASE if event.kind == CostKind.PURCHASE
            else OdometerReading.Source.FUEL if event.kind == CostKind.FUEL
            else OdometerReading.Source.SERVICE if event.kind in SERVICE_KINDS
            else OdometerReading.Source.MANUAL
        )
        OdometerReading.objects.update_or_create(
            vehicle=vehicle, as_of=event.date,
            defaults={"mileage": event.odometer, "source": source},
        )
    if event.kind in SERVICE_KINDS:
        _advance_service_schedules(event)
    _recompute_denorms(vehicle, latest=event)


def _advance_service_schedules(event: VehicleCostEvent):
    """Roll each active schedule forward from a matching service/repair event (date + mileage)."""
    for sched in event.vehicle.service_schedules.filter(is_active=True):
        sched.last_done_date = event.date
        if event.odometer is not None:
            sched.last_done_mileage = event.odometer
        if sched.interval_months:
            sched.next_due_date = _add_months(event.date, sched.interval_months)
        if sched.interval_miles and event.odometer is not None:
            sched.next_due_mileage = event.odometer + sched.interval_miles
        sched.save(update_fields=[
            "last_done_date", "last_done_mileage", "next_due_date", "next_due_mileage",
            "updated_at",
        ])


def _add_months(d: datetime.date, months: int) -> datetime.date:
    import calendar

    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return datetime.date(year, month, day)


def _recompute_denorms(vehicle: Vehicle, *, latest: VehicleCostEvent | None = None):
    """Refresh `current_mileage` (highest odometer reading) and advance any renewal date from a
    cost event's `covers_through`."""
    fields = []
    top = vehicle.odometer_readings.order_by("-mileage").values_list("mileage", flat=True).first()
    if top is not None and top != vehicle.current_mileage:
        vehicle.current_mileage = top
        fields.append("current_mileage")
    if latest is not None and latest.covers_through and latest.kind in RENEWAL_KINDS:
        attr = RENEWAL_KINDS[latest.kind]
        if getattr(vehicle, attr) != latest.covers_through:
            setattr(vehicle, attr, latest.covers_through)
            fields.append(attr)
    if fields:
        vehicle.save(update_fields=[*fields, "updated_at"])


@transaction.atomic
def delete_cost_event(event: VehicleCostEvent, *, user=None):
    """Hard-erase a cost event: delete the module's own payment(s) first, refuse if a FOREIGN
    payment is allocated to the bill, then erase the bill + entry + the event, and recompute
    denorms."""
    from apps.payables.services import delete_bill

    bill = event.bill
    module_pks = set(_module_payments(event).values_list("pk", flat=True))
    _teardown_module_payments(event, user=user)
    if bill is not None:
        foreign = bill.allocations.exclude(payment_id__in=module_pks).exists()
        if foreign:
            raise ValueError(
                "A payment recorded in Payables is allocated to this bill — delete it there first."
            )
        delete_bill(bill, user=user)
        bill.hard_delete()
    vehicle = event.vehicle
    event.hard_delete()
    _recompute_denorms(vehicle)


@transaction.atomic
def void_cost_event(event: VehicleCostEvent, *, user=None):
    """Reverse the cost event's GL impact but keep the record (Void): unpost its bill + tear down
    its payment(s). Used where a closed period blocks an in-place repost."""
    from apps.payables.services import unpost_bill

    _teardown_module_payments(event, user=user)
    if event.bill_id is not None:
        unpost_bill(event.bill, user=user)


# --- Financed purchase settlement ------------------------------------------------------------

@transaction.atomic
def settle_financed_purchase(
    event: VehicleCostEvent, *, down_amount=ZERO, down_source=Funding.BANK,
    down_account=None, down_card=None, loan=None, loan_amount=ZERO, user=None,
):
    """Settle a financed dealer bill: a bank/card down payment + a `Payment.Funding.LOAN`
    disbursement. The loan payment allocates min(loan_amount, remaining) — any excess proceeds are
    a separate bank-funded loans DISBURSEMENT (never over-allocated here). The vehicle links `loan`.
    """
    from apps.payables.models import Payment
    from apps.payables.services import apply_payment

    vehicle = event.vehicle
    ensure_gl_account(vehicle)
    _ensure_vendor_profile(event)
    bill = _sync_bill(event, user=user)
    _teardown_module_payments(event, user=user)  # rebuild funding cleanly on a re-settle

    down_amount = down_amount or ZERO
    remaining = bill.total
    primary = None
    if down_amount > ZERO:
        pay = _new_locked_payment(event)
        pay.amount = down_amount
        pay.funding_kind = _payment_funding(down_source)
        pay.bank_account = down_account if down_source == Funding.BANK else None
        pay.credit_card = down_card if down_source == Funding.CARD else None
        pay.cash_account = event.cash_account if down_source == Funding.CASH else None
        pay.save()
        apply_payment(pay, [(bill, down_amount)], user=user)
        remaining -= down_amount
        primary = pay

    if loan is not None and loan_amount and loan_amount > ZERO and remaining > ZERO:
        alloc = min(loan_amount, remaining)  # residual rule — never over-allocate
        loan_pay = _new_locked_payment(event)
        loan_pay.amount = alloc
        loan_pay.funding_kind = Payment.Funding.LOAN
        loan_pay.loan = loan
        loan_pay.save()
        apply_payment(loan_pay, [(bill, alloc)], user=user)
        primary = primary or loan_pay
        if vehicle.loan_id != loan.pk:
            vehicle.loan = loan
            vehicle.save(update_fields=["loan", "updated_at"])

    if primary is not None and event.payment_id != primary.pk:
        event.payment = primary
        event.save(update_fields=["payment", "updated_at"])
    _apply_event_side_effects(event)
    return event


# --- Multi-vehicle insurance premium ---------------------------------------------------------

@transaction.atomic
def save_insurance_split(
    rows, *, insurer_person=None, insurer_organization=None, date, reference="",
    funding_source=Funding.NONE, funding_account=None, credit_card=None, cash_account=None,
    user=None,
):
    """One insurance document covering several vehicles: create one locked cost event + bill per
    (vehicle, amount, covers_through) row (same insurer vendor, shared document reference), and when
    funded, ONE locked Payment allocated across all N bills (the events share it). `rows` is a list
    of dicts: {vehicle, amount, covers_through}."""
    from apps.payables.models import Payment
    from apps.payables.services import apply_payment

    events = []
    for row in rows:
        amount = row["amount"]
        if not amount or amount <= ZERO:
            continue
        event = VehicleCostEvent.objects.create(
            vehicle=row["vehicle"], kind=CostKind.INSURANCE, date=date, amount=amount,
            vendor_person=insurer_person, vendor_organization=insurer_organization,
            covers_through=row.get("covers_through"), reference=reference,
            funding_source=Funding.NONE,  # the shared payment funds them together (below)
        )
        _ensure_vendor_profile(event)
        _sync_bill(event, user=user)
        _apply_event_side_effects(event)
        events.append(event)

    if not events:
        return []

    funded = funding_source in (Funding.BANK, Funding.CARD, Funding.CASH)
    if funded:
        total = sum((e.amount for e in events), ZERO)
        pay = Payment(
            vendor_person=insurer_person, vendor_organization=insurer_organization,
            date=date, amount=total, funding_kind=_payment_funding(funding_source),
            bank_account=funding_account if funding_source == Funding.BANK else None,
            credit_card=credit_card if funding_source == Funding.CARD else None,
            cash_account=cash_account if funding_source == Funding.CASH else None,
            is_locked=True, source_content_type=_event_ct(),
            source_object_id=events[0].pk,
        )
        pay.save()
        apply_payment(pay, [(e.bill, e.amount) for e in events], user=user)
        VehicleCostEvent.objects.filter(pk__in=[e.pk for e in events]).update(payment=pay)
    return events


# --- Disposal lifecycle ----------------------------------------------------------------------

def _disposal_key(disposal: VehicleDisposal) -> str:
    return f"automobile:disposal:{disposal.pk}:v{disposal.posting_version}"


def _disposal_description(disposal: VehicleDisposal) -> str:
    return f"{disposal.vehicle.nickname}: {disposal.method_label}"


def _disposal_lines(disposal: VehicleDisposal) -> list[LineInput]:
    """The balanced entry for a disposal (see the §1 matrix). Owned: proceeds vs cost, gain/loss to
    4930. Lease return: derecognize the 1320 deposit, refund via 1150, withheld → lease expense."""
    vehicle = disposal.vehicle
    cur = vehicle.currency
    buyer = {"person": disposal.buyer_person, "organization": disposal.buyer_organization}

    def line(account, *, debit=ZERO, credit=ZERO, **party):
        return LineInput(account, debit=debit, credit=credit, currency=cur, **party)

    if disposal.method == DisposalMethod.LEASE_RETURN:
        deposit = vehicle.lease_security_deposit or ZERO
        refund = disposal.proceeds or ZERO
        withheld = deposit - refund
        lines = []
        if refund > ZERO:
            lines.append(line(TRANSFER_CLEARING, debit=refund))
        if withheld > ZERO:
            lines.append(
                line(resolve_posting_account(vehicle, "lease", "vehicle_lease"), debit=withheld)
            )
        lines.append(line(REFUNDABLE_DEPOSITS, credit=deposit))
        return lines

    # Owned disposal — proceeds vs book cost.
    cost = vehicle.cost
    proceeds = disposal.proceeds or ZERO
    if disposal.proceeds_account_id or disposal.method == DisposalMethod.TRADE_IN:
        proceeds_acct = TRANSFER_CLEARING  # routed via 1150 (a bank leg / trade payment clears it)
    else:
        proceeds_acct = CASH_ON_HAND
    lines = []
    if proceeds > ZERO:
        lines.append(line(proceeds_acct, debit=proceeds, **buyer))
    gain = proceeds - cost
    if gain < ZERO:
        lines.append(line(DISPOSAL_GAIN_LOSS, debit=-gain, **buyer))
    elif gain > ZERO:
        lines.append(line(DISPOSAL_GAIN_LOSS, credit=gain, **buyer))
    if cost > ZERO:
        lines.append(line(vehicle.gl_account, credit=cost))
    return lines


def _sync_disposal_bank_leg(disposal: VehicleDisposal, *, user=None):
    """For proceeds/refund to a tracked bank account (not a trade-in), post a native banking
    TRANSFER_IN (Dr bank gl / Cr 1150) so 1150 nets to zero and the bank register stays truthful."""
    if (
        disposal.method == DisposalMethod.TRADE_IN
        or disposal.proceeds_account_id is None
        or (disposal.proceeds or ZERO) <= ZERO
    ):
        return None
    from apps.banking.models import BankTransaction
    from apps.banking.models import TxnType as BankTxnType
    from apps.banking.services import post_transaction as bank_post

    leg = BankTransaction.objects.create(
        account=disposal.proceeds_account, txn_type=BankTxnType.TRANSFER_IN,
        date=disposal.date, amount=disposal.proceeds,
        counter_external=f"{disposal.vehicle.nickname} {disposal.method_label.lower()}",
    )
    bank_post(leg, user=user)
    disposal.bank_txn = leg
    return leg


def _teardown_disposal_bank_leg(disposal: VehicleDisposal, *, user=None):
    if disposal.bank_txn_id is None:
        return
    from apps.banking.services import delete_transaction as delete_bank_txn

    leg = disposal.bank_txn
    disposal.bank_txn = None
    disposal.save(update_fields=["bank_txn", "updated_at"])
    delete_bank_txn(leg, user=user)


def _create_trade_payment(disposal: VehicleDisposal, trade_bill, *, user=None):
    """The trade-in allowance clears 1150 against the replacement vehicle's open dealer bill: a
    locked CASH Payment drawing on 1150 (Dr AP / Cr 1150), so 1150 nets to zero."""
    from apps.payables.models import Payment
    from apps.payables.services import apply_payment

    room = trade_bill.balance_due
    alloc = min(disposal.proceeds or ZERO, room)
    if alloc <= ZERO:
        return None
    pay = Payment(
        vendor_person=trade_bill.vendor_person,
        vendor_organization=trade_bill.vendor_organization,
        date=disposal.date, amount=alloc, funding_kind=Payment.Funding.CASH,
        cash_account=resolve_account(TRANSFER_CLEARING), is_locked=True,
        source_content_type=_disposal_ct(), source_object_id=disposal.pk,
    )
    pay.save()
    apply_payment(pay, [(trade_bill, alloc)], user=user)
    disposal.trade_payment = pay
    return pay


def _disposal_ct():
    from django.contrib.contenttypes.models import ContentType

    return ContentType.objects.get_for_model(VehicleDisposal)


def _teardown_trade_payment(disposal: VehicleDisposal, *, user=None):
    if disposal.trade_payment_id is None:
        return
    from apps.payables.services import delete_payment

    pay = disposal.trade_payment
    disposal.trade_payment = None
    disposal.save(update_fields=["trade_payment", "updated_at"])
    delete_payment(pay, user=user)
    pay.hard_delete()


def _flip_vehicle_disposed(disposal: VehicleDisposal, *, disposed: bool):
    vehicle = disposal.vehicle
    if disposed:
        vehicle.is_active = False
        vehicle.disposed_year = disposal.date.year
        vehicle.disposed_month = disposal.date.month
        vehicle.disposed_day = disposal.date.day
        if disposal.odometer is not None:
            vehicle.current_mileage = max(vehicle.current_mileage or 0, disposal.odometer)
    else:
        vehicle.is_active = True
        vehicle.disposed_year = vehicle.disposed_month = vehicle.disposed_day = None
    vehicle.save(update_fields=[
        "is_active", "disposed_year", "disposed_month", "disposed_day", "current_mileage",
        "updated_at",
    ])


@transaction.atomic
def post_disposal(disposal: VehicleDisposal, *, trade_bill=None, user=None):
    """Post the disposal's direct entry, its banking leg (proceeds→bank) or trade-in clearing, and
    flip the vehicle to disposed/inactive."""
    lines = _disposal_lines(disposal)
    if len(lines) >= 2:
        entry = post_entry(
            date=disposal.date, lines=lines, source=disposal,
            external_key=_disposal_key(disposal), description=_disposal_description(disposal),
            memo=disposal.notes, user=user,
        )
        disposal.journal_entry = entry
    _sync_disposal_bank_leg(disposal, user=user)
    if disposal.method == DisposalMethod.TRADE_IN and trade_bill is not None:
        _create_trade_payment(disposal, trade_bill, user=user)
    disposal.save()
    _flip_vehicle_disposed(disposal, disposed=True)
    return disposal


@transaction.atomic
def repost_disposal(disposal: VehicleDisposal, *, trade_bill=None, user=None):
    """Reverse the current entry, tear down its legs, bump the version, and rebuild (edit path)."""
    _teardown_disposal_bank_leg(disposal, user=user)
    _teardown_trade_payment(disposal, user=user)
    current = disposal.journal_entry
    if current is not None and current.status == JournalEntry.Status.POSTED:
        reverse_entry(current, user=user)
    disposal.posting_version += 1
    disposal.journal_entry = None
    disposal.save(update_fields=["posting_version", "journal_entry", "updated_at"])
    return post_disposal(disposal, trade_bill=trade_bill, user=user)


@transaction.atomic
def unpost_disposal(disposal: VehicleDisposal, *, user=None):
    """Reverse the disposal (keep the record) and restore the vehicle to active."""
    _teardown_trade_payment(disposal, user=user)
    if disposal.bank_txn_id is not None:
        from apps.banking.services import unpost_transaction as unpost_bank_txn

        unpost_bank_txn(disposal.bank_txn, user=user)
    current = disposal.journal_entry
    if current is not None and current.status == JournalEntry.Status.POSTED:
        reverse_entry(current, user=user)
    _flip_vehicle_disposed(disposal, disposed=False)


@transaction.atomic
def delete_disposal(disposal: VehicleDisposal, *, user=None):
    """Hard-erase the disposal, its GL entry, bank leg and trade payment; restore the vehicle."""
    _teardown_disposal_bank_leg(disposal, user=user)
    _teardown_trade_payment(disposal, user=user)
    entry = disposal.journal_entry
    _flip_vehicle_disposed(disposal, disposed=False)
    if entry is not None:
        entry.hard_delete()
    disposal.hard_delete()


# --- Driver ↔ party ("insured" / dealer "customer" P2O) synchronisation ----------------------

def sync_driver_p2o(vehicle: Vehicle, *, user=None) -> None:
    """Ensure each driver has an org-level 'insured' link to the insurer, and each owner a
    'customer' link to the dealer. Add-only; no-ops when the type isn't seeded / no org is set."""
    from apps.automobile.models import OWNER_ROLES
    from apps.relationships.models import PersonOrgRelationship, PersonOrgRelationshipType

    insured = PersonOrgRelationshipType.objects.filter(code="insured").first()
    customer = PersonOrgRelationshipType.objects.filter(code="customer").first()
    drivers = list(vehicle.drivers.select_related("person").all())
    if vehicle.insurer_organization_id and insured is not None:
        for d in drivers:
            PersonOrgRelationship.objects.get_or_create(
                person=d.person, organization=vehicle.insurer_organization, type=insured
            )
    if vehicle.dealer_organization_id and customer is not None:
        for d in drivers:
            if d.role in OWNER_ROLES:
                PersonOrgRelationship.objects.get_or_create(
                    person=d.person, organization=vehicle.dealer_organization, type=customer
                )


# --- Read models (pure; post nothing) --------------------------------------------------------

def current_mileage(vehicle: Vehicle):
    """The highest recorded odometer reading (falls back to the denormalized field)."""
    top = vehicle.odometer_readings.order_by("-mileage").values_list("mileage", flat=True).first()
    return top if top is not None else vehicle.current_mileage


def mileage_log(vehicle: Vehicle) -> list[OdometerReading]:
    return list(vehicle.odometer_readings.order_by("-as_of", "-id"))


def fuel_economy(vehicle: Vehicle) -> list[dict]:
    """Economy between consecutive FULL-tank fills, summing the volumes of ALL fills (partials
    included) since the previous full fill; segments missing an odometer are skipped. One summary
    per fuel unit (PHEV: no blended metric in v1). mi/gal, L/100km, or distance-per-kWh by unit."""
    fills = list(
        vehicle.cost_events.filter(kind=CostKind.FUEL, fuel_volume__isnull=False)
        .order_by("date", "id")
    )
    by_unit: dict[str, list] = {}
    for f in fills:
        by_unit.setdefault(f.fuel_unit or "", []).append(f)

    unit_km = vehicle.mileage_unit == "km"
    results = []
    for unit, group in by_unit.items():
        segments = []
        prev_full = None
        vol_accum = ZERO
        for f in group:
            vol_accum += f.fuel_volume or ZERO
            if f.is_full_tank:
                if (
                    prev_full is not None and f.odometer is not None
                    and prev_full.odometer is not None
                ):
                    dist = f.odometer - prev_full.odometer
                    if dist > 0 and vol_accum > 0:
                        segments.append((Decimal(dist), vol_accum))
                prev_full = f
                vol_accum = ZERO
        if not segments:
            continue
        total_dist = sum((d for d, _ in segments), ZERO)
        total_vol = sum((v for _, v in segments), ZERO)
        if total_vol <= ZERO or total_dist <= ZERO:
            continue
        if unit == "l" and unit_km:
            economy = (total_vol / total_dist * 100).quantize(Decimal("0.1"))
            label = "L/100km"
        else:
            economy = (total_dist / total_vol).quantize(Decimal("0.1"))
            dist_unit = "km" if unit_km else "mi"
            fuel_label = {"gal": "gal", "l": "L", "kWh": "kWh"}.get(unit, unit or "unit")
            label = f"{dist_unit}/{fuel_label}"
        results.append({
            "unit": unit, "label": label, "economy": economy,
            "distance": total_dist, "volume": total_vol, "fills": len(group),
        })
    return results


def _financing_interest(vehicle: Vehicle) -> Decimal:
    """Lifetime interest booked on this vehicle's linked loan (pure read from Loans; the GL expense
    stays in Loans — this is TCO attribution, not double-booking)."""
    if not (vehicle.is_financed and vehicle.loan_id):
        return ZERO
    from apps.loans.services import interest_by_year

    return interest_by_year(vehicle.loan).get("total", ZERO)


def running_cost_total(vehicle: Vehicle) -> Decimal:
    """Lifetime running cost (fuel + service + insurance + …), plus financing interest."""
    total = sum(
        (e.amount for e in vehicle.cost_events.filter(kind__in=list(RUNNING_COST_KINDS))),
        ZERO,
    )
    return total + _financing_interest(vehicle)


def cost_per_mile(vehicle: Vehicle):
    """Lifetime running cost ÷ miles driven since acquisition (None until we can compute it)."""
    miles = vehicle.lease_mileage_used if vehicle.is_leased else None
    if miles is None:
        cur = current_mileage(vehicle)
        base = vehicle.initial_mileage
        if cur is None or base is None or cur <= base:
            return None
        miles = cur - base
    if not miles or miles <= 0:
        return None
    return (running_cost_total(vehicle) / Decimal(miles)).quantize(Decimal("0.01"))


def next_service_due(vehicle: Vehicle):
    """The soonest active service schedule (overdue first), or None."""
    scheds = [s for s in vehicle.service_schedules.filter(is_active=True) if s.next_due_date]
    scheds.sort(key=lambda s: s.next_due_date)
    return scheds[0] if scheds else None


RENEWAL_LABELS = [
    ("insurance_expiry", "Insurance", "shield"),
    ("registration_expiry", "Registration", "file-text"),
    ("inspection_due", "Inspection", "clipboard-check"),
    ("lease_end_date", "Lease ends", "calendar-days"),
    ("warranty_expiry", "Warranty ends", "shield-check"),
]


def renewals_due(within_days: int = 90) -> list[dict]:
    """Upcoming renewals across active, non-disposed vehicles (insurance / registration / inspection
    / lease end / warranty), soonest first (past-due-but-open first). A pure read."""
    today = datetime.date.today()
    horizon = today + datetime.timedelta(days=within_days)
    rows = []
    for vehicle in Vehicle.objects.filter(is_active=True):
        if vehicle.is_disposed:
            continue
        for attr, label, glyph in RENEWAL_LABELS:
            when = getattr(vehicle, attr)
            if when is None or when > horizon:
                continue
            rows.append({
                "vehicle": vehicle, "label": label, "glyph": glyph, "date": when,
                "days": (when - today).days,
            })
    rows.sort(key=lambda r: r["date"])
    return rows


def depreciation_series(vehicle: Vehicle) -> dict:
    """A (date, cost, value) series for the value chart: `cost` from the GL as-of each key date
    (step function of capitalizing events), `value` = the latest valuation carried forward, else
    cost. Shaped for `investments.services.line_chart_points`."""
    valuations = list(vehicle.valuations.order_by("as_of"))
    cap_events = list(
        vehicle.cost_events.filter(kind__in=list(CAPITALIZING_KINDS)).order_by("date")
    )
    dates = {v.as_of for v in valuations} | {e.date for e in cap_events}
    if vehicle.acquired.is_set and vehicle.acquired.year:
        dates.add(datetime.date(
            vehicle.acquired.year, vehicle.acquired.month or 1, vehicle.acquired.day or 1
        ))
    today = datetime.date.today()
    dates.add(today)
    ordered = sorted(d for d in dates if d <= today)
    if len(ordered) < 2:
        ordered = [today]

    def cost_on(d):
        if vehicle.is_owned and vehicle.gl_account_id is not None:
            return account_balance(vehicle.gl_account, as_of=d)
        return vehicle.cost_basis or ZERO

    def value_on(d):
        latest = None
        for v in valuations:
            if v.as_of <= d:
                latest = v.value
        return latest

    series = []
    for d in ordered:
        cost = cost_on(d)
        value = value_on(d)
        series.append((d, cost, value if value is not None else cost))
    vals = [x for _, c, m in series for x in (c, m)]
    last_cost = series[-1][1] if series else ZERO
    last_value = series[-1][2] if series else ZERO
    return {
        "series": series,
        "min": min(vals) if vals else ZERO,
        "max": max(vals) if vals else ZERO,
        "last_cost": last_cost,
        "last_value": last_value,
        "gain": last_value - last_cost,  # negative = depreciation below cost
    }


def register(vehicle: Vehicle) -> list[VehicleCostEvent]:
    """The vehicle's cost-event register, newest first."""
    return list(
        vehicle.cost_events.select_related(
            "vendor_person", "vendor_organization", "bill", "payment"
        ).order_by("-date", "-id")
    )


def cost_by_category(vehicle: Vehicle):
    """(c-donut segments, total) of lifetime running cost grouped by kind, + a Financing interest
    slice for a financed vehicle."""
    from apps.investments.services import Slice, donut_segments

    tints = {
        CostKind.FUEL: "amber", CostKind.SERVICE: "teal", CostKind.REPAIR: "rose",
        CostKind.INSURANCE: "sky", CostKind.REGISTRATION: "violet",
        CostKind.INSPECTION: "indigo", CostKind.LEASE_PAYMENT: "emerald",
        CostKind.TAX_FEE: "slate", CostKind.OTHER: "slate",
    }
    labels = dict(CostKind.choices)
    buckets: dict = {}
    for e in vehicle.cost_events.filter(kind__in=list(RUNNING_COST_KINDS)):
        buckets[e.kind] = buckets.get(e.kind, ZERO) + e.amount
    slices = [
        Slice(labels.get(k, k), v, tints.get(k, "slate"))
        for k, v in buckets.items() if v > ZERO
    ]
    interest = _financing_interest(vehicle)
    if interest > ZERO:
        slices.append(Slice("Financing interest", interest, "rose"))
    slices.sort(key=lambda s: s.value, reverse=True)
    return donut_segments(slices), sum((s.value for s in slices), ZERO)


def monthly_running_cost(vehicles=None) -> Decimal:
    """Estimated monthly running cost across active vehicles: trailing-365-day running costs ÷ 12,
    plus each financed vehicle's monthly loan interest run-rate."""
    if vehicles is None:
        vehicles = Vehicle.objects.filter(is_active=True)
    since = datetime.date.today() - datetime.timedelta(days=365)
    total = ZERO
    for vehicle in vehicles:
        spent = sum(
            (e.amount for e in vehicle.cost_events.filter(
                kind__in=list(RUNNING_COST_KINDS), date__gte=since
            )),
            ZERO,
        )
        total += spent / 12
    return total.quantize(Decimal("0.01"))


def fleet_value(vehicles=None) -> Decimal:
    """Total book cost of active owned vehicles (leased vehicles hold no asset)."""
    if vehicles is None:
        vehicles = Vehicle.objects.filter(is_active=True)
    return sum((v.cost for v in vehicles if v.is_owned), ZERO)


def _cost_ytd(vehicles) -> Decimal:
    year_start = datetime.date(datetime.date.today().year, 1, 1)
    total = ZERO
    for vehicle in vehicles:
        total += sum(
            (e.amount for e in vehicle.cost_events.filter(
                kind__in=list(RUNNING_COST_KINDS), date__gte=year_start
            )),
            ZERO,
        )
    return total


def dashboard_stats() -> dict:
    """Headline figures for the Vehicles dashboard."""
    vehicles = list(Vehicle.objects.filter(is_active=True).select_related("currency", "gl_account"))
    owned = [v for v in vehicles if v.is_owned]
    leased = [v for v in vehicles if v.is_leased]
    return {
        "vehicles": vehicles,
        "vehicles_count": len(vehicles),
        "owned_count": len(owned),
        "leased_count": len(leased),
        "fleet_value": fleet_value(vehicles),
        "monthly_running_cost": monthly_running_cost(vehicles),
        "cost_ytd": _cost_ytd(vehicles),
    }


def launcher_counts() -> list[dict]:
    """Live counts for the launcher tile: Vehicles / Fleet value / renewals due soon."""
    vehicles = list(Vehicle.objects.filter(is_active=True))
    due = renewals_due(within_days=45)
    return [
        {"n": len(vehicles), "label": "Vehicles"},
        {"n": fleet_value(vehicles), "label": "Fleet value"},
        {"n": len(due), "label": "Due soon"},
    ]
