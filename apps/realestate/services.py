"""Real Estate service layer — the only sanctioned GL path for the Real Estate module.

Two GL-touching flows, both through existing service layers (never hand-written ledger rows):

1. **Cost events → locked Payables bills (+ optional locked Payments).** A `PropertyCostEvent`
   materializes a `payables.Bill` (`is_locked=True`, `source=<event>`, one line to the right GL
   account) via `payables.services.post_bill`; when funded, a locked `payables.Payment`
   (BANK/CARD/CASH) allocated to the bill. A financed purchase settles the seller bill with a bank
   down-payment + a `Payment.Funding.LOAN` mortgage disbursement.
2. **Disposal → a direct finance entry** (`PropertyDisposal`) booking proceeds vs book cost with the
   difference to `4930`. Proceeds to a tracked bank route via `1150` + a native banking TRANSFER_IN.

An **owned** property owns one postable ASSET node under `1410 Real Estate` (`ensure_gl_account`),
so its cost is `account_balance(gl_account)`. The `PropertyValuation` overlay posts
nothing. Property tax posts to the GENERIC `5810` (property_tax_expense) — never the `5140`
mortgage-escrow home tax, which the Loans module already books from a mortgage payment.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from apps.finance.models import ZERO, Account, AccountType, JournalEntry, Side
from apps.finance.services import (
    LineInput,
    post_entry,
    resolve_account,
    resolve_posting_account,
    reverse_entry,
)
from apps.realestate.models import (
    CAPITALIZING_KINDS,
    KIND_ACTIVITY,
    CostKind,
    Funding,
    Property,
    PropertyCostEvent,
    PropertyDisposal,
)

# Fixed accounts (resolved by stable system_key / code). Structural legs are never remappable.
REAL_ESTATE_HEADER = "1410"                       # Real Estate (per-property nodes nest under it)
DISPOSAL_GAIN_LOSS = "asset_disposal_gain_loss"   # 4930
TRANSFER_CLEARING = "transfer_clearing"           # 1150
CASH_ON_HAND = "1110"

# Category activities the Expert-mode Accounting tab can remap, per property. Structural legs (the
# asset node 1410.NN) are never here — they're capitalizing, not expenses.
POSTING_ACTIVITIES = [
    {"key": "property_tax", "label": "Property tax", "kind": "expense",
     "default": "property_tax_expense"},
    {"key": "maintenance", "label": "Maintenance / repair", "kind": "expense", "default": "5130"},
    {"key": "hoa", "label": "HOA / condo fees", "kind": "expense", "default": "hoa_fees"},
    {"key": "utilities", "label": "Utilities", "kind": "expense", "default": "5120"},
]


# --- GL account provisioning (one node per property) -----------------------------------------

def _gl_name(property: Property) -> str:
    return property.nickname


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


def ensure_gl_account(property: Property, *, parent=None, existing=None) -> Account:
    """Create (or reconcile) the postable ASSET node carrying this property's cost, nested under
    `1410 Real Estate`. After disposal the node nets to 0 but is kept (it has posted lines)."""
    if property.gl_account_id:
        gl = property.gl_account
        changed = []
        name = _gl_name(property)
        if gl.name != name:
            gl.name = name
            changed.append("name")
        if gl.currency_id != property.currency_id and not gl.lines.exists():
            gl.currency = property.currency
            changed.append("currency")
        if changed:
            gl.save(update_fields=[*changed, "updated_at"])
        return gl

    if existing is not None:
        property.gl_account = existing
        property.save(update_fields=["gl_account"])
        return existing

    parent = parent or resolve_account(REAL_ESTATE_HEADER)
    gl = Account.objects.create(
        code=_next_child_code(parent),
        name=_gl_name(property),
        type=AccountType.ASSET,
        normal_side=Side.DEBIT,
        currency=property.currency,
        parent=parent,
        is_postable=True,
        is_system=False,
    )
    property.gl_account = gl
    property.save(update_fields=["gl_account"])
    return gl


def _bill_line_account(event: PropertyCostEvent) -> Account:
    """The GL account a cost event's single bill line posts to. Capitalizing kinds are structural
    (the property node) and never remappable; expense kinds honor the per-property posting map in
    Expert mode."""
    property = event.property
    if event.kind in CAPITALIZING_KINDS:  # purchase / improvement / closing costs → the node
        return ensure_gl_account(property)
    key, default = KIND_ACTIVITY.get(event.kind, (None, "5900"))
    if key is None:
        return resolve_account(default)
    return resolve_posting_account(property, key, default)


# --- Vendor tagging (reuse the Payables catalog) ---------------------------------------------

def _ensure_vendor_category(org) -> None:
    from apps.setup.models import Category

    cat = Category.objects.filter(kind=Category.Kind.ORG, name="Vendor").first()
    if cat:
        org.categories.add(cat)


def _ensure_vendor_profile(event: PropertyCostEvent) -> None:
    from apps.payables.services import ensure_vendor_profile

    if event.vendor_organization_id:
        _ensure_vendor_category(event.vendor_organization)
        ensure_vendor_profile(organization=event.vendor_organization)
    elif event.vendor_person_id:
        ensure_vendor_profile(person=event.vendor_person)


# --- Locked bill + payment sync --------------------------------------------------------------

def _event_ct():
    from django.contrib.contenttypes.models import ContentType

    return ContentType.objects.get_for_model(PropertyCostEvent)


def _bill_line_desc(event: PropertyCostEvent) -> str:
    property = event.property
    if event.kind == CostKind.PURCHASE:
        return f"{property.nickname} — purchase"
    if event.memo:
        return event.memo
    return f"{property.nickname} — {event.get_kind_display().lower()}"


def _sync_bill(event: PropertyCostEvent, *, user=None):
    """Create (or repost in place) the locked Payables bill backing this cost event — one line to
    the right GL account, tagged as sourced from this event."""
    from apps.payables.models import Bill, BillLine
    from apps.payables.services import post_bill, repost_bill

    property = event.property
    bill = event.bill or Bill(is_locked=True)
    bill.vendor_person = event.vendor_person
    bill.vendor_organization = event.vendor_organization
    bill.bill_date = event.date
    bill.due_date = event.due_date or event.date
    bill.currency = property.currency
    bill.vendor_ref = event.reference
    bill.notes = event.memo
    bill.is_locked = True
    bill.source_content_type = _event_ct()
    bill.source_object_id = event.pk
    bill.save()

    bill.lines.all().delete()  # single-line, rewritten each save
    BillLine.objects.create(
        bill=bill, line_type=BillLine.LineType.EXPENSE, order=0,
        description=_bill_line_desc(event), account=_bill_line_account(event),
        quantity=Decimal("1"), unit_price=event.amount,
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


def _module_payments(event: PropertyCostEvent):
    """Every locked Payables payment sourced to this event (a funded cost event has one; a financed
    purchase has a down payment + a loan payment)."""
    from apps.payables.models import Payment

    return Payment.objects.filter(source_content_type=_event_ct(), source_object_id=event.pk)


def _teardown_module_payments(event: PropertyCostEvent, *, user=None):
    from apps.payables.services import delete_payment

    for pay in list(_module_payments(event)):
        delete_payment(pay, user=user)
        pay.hard_delete()
    if event.payment_id is not None:
        event.payment = None
        event.save(update_fields=["payment", "updated_at"])


def _new_locked_payment(event: PropertyCostEvent):
    from apps.payables.models import Payment

    return Payment(
        vendor_person=event.vendor_person,
        vendor_organization=event.vendor_organization,
        date=event.date,
        is_locked=True,
        source_content_type=_event_ct(),
        source_object_id=event.pk,
    )


def _sync_single_payment(event: PropertyCostEvent, *, user=None):
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
def save_cost_event(event: PropertyCostEvent, *, user=None, is_new=True):
    """Post a saved cost event: ensure the GL node (capitalizing), tag the vendor, build the locked
    bill and (if funded) the locked payment. The caller has resolved + set the vendor party and
    saved the event row (so it has a pk)."""
    if event.kind in CAPITALIZING_KINDS:
        ensure_gl_account(event.property)
    _ensure_vendor_profile(event)
    _sync_bill(event, user=user)
    _sync_single_payment(event, user=user)
    return event


@transaction.atomic
def delete_cost_event(event: PropertyCostEvent, *, user=None):
    """Hard-erase a cost event: delete the module's own payment(s) first, refuse if a FOREIGN
    payment is allocated to the bill, then erase the bill + entry + the event."""
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
    event.hard_delete()


@transaction.atomic
def void_cost_event(event: PropertyCostEvent, *, user=None):
    """Reverse the cost event's GL impact but keep the record (Void): unpost its bill + tear down
    its payment(s)."""
    from apps.payables.services import unpost_bill

    _teardown_module_payments(event, user=user)
    if event.bill_id is not None:
        unpost_bill(event.bill, user=user)


@transaction.atomic
def settle_financed_purchase(
    event: PropertyCostEvent, *, down_amount=ZERO, down_source=Funding.BANK,
    down_account=None, down_card=None, loan=None, loan_amount=ZERO, user=None,
):
    """Settle a financed seller bill: a bank/card down payment + a `Payment.Funding.LOAN` mortgage
    disbursement. The loan payment allocates min(loan_amount, remaining) — never over-allocated. The
    property links `loan` as its mortgage."""
    from apps.payables.models import Payment
    from apps.payables.services import apply_payment

    property = event.property
    ensure_gl_account(property)
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
        if property.mortgage_loan_id != loan.pk:
            property.mortgage_loan = loan
            property.save(update_fields=["mortgage_loan", "updated_at"])

    if primary is not None and event.payment_id != primary.pk:
        event.payment = primary
        event.save(update_fields=["payment", "updated_at"])
    return event


# --- Disposal lifecycle ----------------------------------------------------------------------

def _disposal_key(disposal: PropertyDisposal) -> str:
    return f"realestate:disposal:{disposal.pk}:v{disposal.posting_version}"


def _disposal_description(disposal: PropertyDisposal) -> str:
    return f"{disposal.property.nickname}: {disposal.method_label}"


def _disposal_lines(disposal: PropertyDisposal) -> list[LineInput]:
    """The balanced entry for a disposal: proceeds vs book cost, gain/loss to 4930. Proceeds to a
    tracked bank route via 1150 (a native TRANSFER_IN clears it); else Cash on Hand. The mortgage is
    untouched here (payoff is a separate Loans-module action)."""
    property = disposal.property
    cur = property.currency
    buyer = {"person": disposal.buyer_person, "organization": disposal.buyer_organization}

    def line(account, *, debit=ZERO, credit=ZERO, **party):
        return LineInput(account, debit=debit, credit=credit, currency=cur, **party)

    cost = property.cost
    proceeds = disposal.proceeds or ZERO
    proceeds_acct = TRANSFER_CLEARING if disposal.proceeds_account_id else CASH_ON_HAND
    lines = []
    if proceeds > ZERO:
        lines.append(line(proceeds_acct, debit=proceeds, **buyer))
    gain = proceeds - cost
    if gain < ZERO:
        lines.append(line(DISPOSAL_GAIN_LOSS, debit=-gain, **buyer))
    elif gain > ZERO:
        lines.append(line(DISPOSAL_GAIN_LOSS, credit=gain, **buyer))
    if cost > ZERO:
        lines.append(line(property.gl_account, credit=cost))
    return lines


def _disposal_ct():
    from django.contrib.contenttypes.models import ContentType

    return ContentType.objects.get_for_model(PropertyDisposal)


def _sync_disposal_bank_leg(disposal: PropertyDisposal, *, user=None):
    """For proceeds to a tracked bank account, post a native banking TRANSFER_IN (Dr bank gl / Cr
    1150) so 1150 nets to zero and the bank register stays truthful."""
    if disposal.proceeds_account_id is None or (disposal.proceeds or ZERO) <= ZERO:
        return None
    from apps.banking.models import BankTransaction
    from apps.banking.models import TxnType as BankTxnType
    from apps.banking.services import post_transaction as bank_post

    leg = BankTransaction.objects.create(
        account=disposal.proceeds_account, txn_type=BankTxnType.TRANSFER_IN,
        date=disposal.date, amount=disposal.proceeds,
        counter_external=f"{disposal.property.nickname} {disposal.method_label.lower()}",
    )
    bank_post(leg, user=user)
    disposal.bank_txn = leg
    return leg


def _teardown_disposal_bank_leg(disposal: PropertyDisposal, *, user=None):
    if disposal.bank_txn_id is None:
        return
    from apps.banking.services import delete_transaction as delete_bank_txn

    leg = disposal.bank_txn
    disposal.bank_txn = None
    disposal.save(update_fields=["bank_txn", "updated_at"])
    delete_bank_txn(leg, user=user)


def _flip_property_disposed(disposal: PropertyDisposal, *, disposed: bool):
    property = disposal.property
    if disposed:
        property.is_active = False
        property.disposed_year = disposal.date.year
        property.disposed_month = disposal.date.month
        property.disposed_day = disposal.date.day
    else:
        property.is_active = True
        property.disposed_year = property.disposed_month = property.disposed_day = None
    property.save(update_fields=[
        "is_active", "disposed_year", "disposed_month", "disposed_day", "updated_at",
    ])


@transaction.atomic
def post_disposal(disposal: PropertyDisposal, *, user=None):
    """Post the disposal's direct entry, its banking leg (proceeds→bank), and flip the property to
    disposed/inactive."""
    lines = _disposal_lines(disposal)
    if len(lines) >= 2:
        entry = post_entry(
            date=disposal.date, lines=lines, source=disposal,
            external_key=_disposal_key(disposal), description=_disposal_description(disposal),
            memo=disposal.notes, user=user,
        )
        disposal.journal_entry = entry
    _sync_disposal_bank_leg(disposal, user=user)
    disposal.save()
    _flip_property_disposed(disposal, disposed=True)
    return disposal


@transaction.atomic
def repost_disposal(disposal: PropertyDisposal, *, user=None):
    """Reverse the current entry, tear down its bank leg, bump the version, and rebuild (edit path).
    """
    _teardown_disposal_bank_leg(disposal, user=user)
    current = disposal.journal_entry
    if current is not None and current.status == JournalEntry.Status.POSTED:
        reverse_entry(current, user=user)
    disposal.posting_version += 1
    disposal.journal_entry = None
    disposal.save(update_fields=["posting_version", "journal_entry", "updated_at"])
    return post_disposal(disposal, user=user)


@transaction.atomic
def unpost_disposal(disposal: PropertyDisposal, *, user=None):
    """Reverse the disposal (keep the record) and restore the property to active."""
    if disposal.bank_txn_id is not None:
        from apps.banking.services import unpost_transaction as unpost_bank_txn

        unpost_bank_txn(disposal.bank_txn, user=user)
    current = disposal.journal_entry
    if current is not None and current.status == JournalEntry.Status.POSTED:
        reverse_entry(current, user=user)
    _flip_property_disposed(disposal, disposed=False)


@transaction.atomic
def delete_disposal(disposal: PropertyDisposal, *, user=None):
    """Hard-erase the disposal, its GL entry and bank leg; restore the property."""
    _teardown_disposal_bank_leg(disposal, user=user)
    entry = disposal.journal_entry
    _flip_property_disposed(disposal, disposed=False)
    if entry is not None:
        entry.hard_delete()
    disposal.hard_delete()


# --- Read models (pure; post nothing) --------------------------------------------------------

def _active_properties():
    return Property.objects.filter(is_active=True)


def portfolio_cost() -> Decimal:
    return sum((p.cost for p in _active_properties()), ZERO)


def portfolio_value() -> Decimal:
    return sum((p.current_value for p in _active_properties()), ZERO)


def dashboard_stats() -> dict:
    """Headline figures for the Real Estate dashboard."""
    properties = list(
        Property.objects.select_related("currency", "mortgage_loan").filter(is_active=True)
    )
    cost = sum((p.cost for p in properties), ZERO)
    value = sum((p.current_value for p in properties), ZERO)
    return {
        "properties": properties,
        "properties_count": len(properties),
        "portfolio_cost": cost,
        "portfolio_value": value,
        "portfolio_appreciation": value - cost,
    }


def launcher_counts() -> list[dict]:
    """Live counts for the launcher tile: properties / portfolio value at market."""
    props = _active_properties()
    return [
        {"n": props.count(), "label": "Properties"},
        {"n": portfolio_value(), "label": "Market value"},
    ]
