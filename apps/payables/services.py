"""Payables service layer — the ONLY sanctioned path to the general ledger, plus subledger helpers.

A bill posts accrual double-entry through `apps.finance.services` (never direct JournalEntry/Line
rows): each line DRs an expense/asset (shipping/tax to their own accounts, a discount CRs Purchase
Discounts) and the bill total CRs Accounts Payable, tagged with the vendor party. Bills post on save
and are edited **in place** via `finance.repost_entry` (no reversal); deleting reverses. Capitalized
lines materialize an `AssetItem` (held at cost, warranty-tracked).
"""

import datetime
from decimal import Decimal

from apps.finance.models import JournalEntry
from apps.finance.services import (
    LineInput,
    post_entry,
    repost_entry,
    resolve_account,
    reverse_entry,
)
from apps.payables.models import (
    AssetItem,
    Bill,
    BillLine,
    Payment,
    PaymentAllocation,
    VendorProfile,
)

ZERO = Decimal("0")
_OPEN = [Bill.Status.OPEN, Bill.Status.PARTIALLY_PAID]

_CATEGORY_TYPES = {BillLine.LineType.ITEM, BillLine.LineType.SERVICE, BillLine.LineType.EXPENSE}


# --- Vendors ---------------------------------------------------------------------------------

def ensure_vendor_profile(*, person=None, organization=None) -> VendorProfile:
    """Get-or-create the VendorProfile for a Person or an Organization (exactly one)."""
    if person is not None:
        profile, _ = VendorProfile.objects.get_or_create(person=person)
    else:
        profile, _ = VendorProfile.objects.get_or_create(organization=organization)
    return profile


def vendor_balance(vendor_profile) -> Decimal:
    """Amount currently owed to a vendor — Σ balance due over their non-void bills."""
    qs = Bill.objects.exclude(status=Bill.Status.VOID)
    qs = (
        qs.filter(vendor_person=vendor_profile.person)
        if vendor_profile.person_id
        else qs.filter(vendor_organization=vendor_profile.organization)
    )
    return sum((b.balance_due for b in qs), ZERO)


# --- Bill posting (accrual; in-place edits) --------------------------------------------------

def _bill_key(bill) -> str:
    return f"payables:bill:{bill.pk}:v{bill.posting_version}"


def _line_account(line: BillLine):
    """The GL account a bill line posts to (default by line type, overridable per line/item)."""
    lt = line.line_type
    if lt == BillLine.LineType.SHIPPING:
        return resolve_account(line.account or "shipping_expense")
    if lt == BillLine.LineType.TAX:
        return resolve_account(line.account or "sales_tax_paid")
    if line.capitalize:
        if line.account_id:
            return line.account
        if line.item_id and line.item.asset_account_id:
            return line.item.asset_account
        return resolve_account("household_goods")
    if line.account_id:
        return line.account
    if line.item_id and line.item.default_account_id:
        return line.item.default_account
    return resolve_account("5900")  # Other Expenses fallback


def _bill_lines(bill) -> list[LineInput]:
    """The balanced double-entry for a bill: each line DRs its account (a discount CRs Purchase
    Discounts); the total CRs Accounts Payable, tagged with the vendor party."""
    party = {"person": bill.vendor_person, "organization": bill.vendor_organization}
    cur = bill.currency
    lines: list[LineInput] = []
    for line in bill.lines.all():
        amt = line.amount
        if amt == ZERO:
            continue
        if line.line_type == BillLine.LineType.DISCOUNT:
            lines.append(LineInput(account=resolve_account("purchase_discounts"),
                                   credit=abs(amt), currency=cur, memo=line.description))
        else:
            lines.append(LineInput(account=_line_account(line), debit=amt, currency=cur,
                                   memo=line.description))
    lines.append(LineInput(account="accounts_payable", credit=bill.total, currency=cur, **party))
    return lines


def _description(bill) -> str:
    return f"{bill.bill_number} — {bill.vendor_name}"


def post_bill(bill, *, user=None) -> JournalEntry:
    """Post a bill's accrual entry (DR expense/asset / CR AP) and link it back. Called on save."""
    entry = post_entry(
        date=bill.bill_date,
        lines=_bill_lines(bill),
        description=_description(bill),
        currency=bill.currency,
        source=bill,
        external_key=_bill_key(bill),
        user=user,
    )
    bill.journal_entry = entry
    bill.save(update_fields=["journal_entry", "updated_at"])
    _sync_asset_items(bill)
    recompute_bill_status(bill)
    return entry


def repost_bill(bill, *, user=None) -> JournalEntry:
    """Edit a posted bill IN PLACE — rewrite its journal lines with no reversal (via
    finance.repost_entry). Falls back to a fresh post if the bill has no entry yet."""
    if not bill.journal_entry_id:
        return post_bill(bill, user=user)
    repost_entry(
        bill.journal_entry, lines=_bill_lines(bill), date=bill.bill_date,
        description=_description(bill), user=user,
    )
    _sync_asset_items(bill)
    recompute_bill_status(bill)
    return bill.journal_entry


def unpost_bill(bill, *, user=None) -> None:
    """Undo a bill's GL impact (on delete): reverse its posted entry and drop its asset items."""
    if bill.journal_entry_id and bill.journal_entry.status == JournalEntry.Status.POSTED:
        reverse_entry(bill.journal_entry, user=user)
    AssetItem.objects.filter(bill_line__bill=bill).delete()


def recompute_bill_status(bill) -> None:
    """Flip Open → Partially Paid → Paid from the applied payment total (void is sticky)."""
    if bill.status == Bill.Status.VOID:
        return
    paid = bill.amount_paid
    if paid <= ZERO:
        status = Bill.Status.OPEN
    elif paid < bill.total:
        status = Bill.Status.PARTIALLY_PAID
    else:
        status = Bill.Status.PAID
    if status != bill.status:
        bill.status = status
        bill.save(update_fields=["status", "updated_at"])


def _sync_asset_items(bill) -> None:
    """Create/refresh an AssetItem per capitalized line; drop assets for un-capitalized lines."""
    kept = []
    for line in bill.lines.all():
        if not line.capitalize or line.amount <= ZERO or line.line_type not in _CATEGORY_TYPES:
            continue
        asset = AssetItem.all_objects.filter(bill_line=line).first() or AssetItem()
        if asset.pk and asset.deleted_at:
            asset.restore()
        asset.name = line.description or (line.item.name if line.item_id else "Asset")
        asset.vendor_name = bill.vendor_name
        asset.serial_number = line.asset_serial
        asset.purchase_date = bill.bill_date
        asset.warranty_start = bill.bill_date
        asset.warranty_end = line.warranty_end
        asset.cost = line.amount
        asset.gl_account = _line_account(line)
        asset.bill_line = line
        asset.save()
        kept.append(asset.pk)
    AssetItem.objects.filter(bill_line__bill=bill).exclude(pk__in=kept).delete()


# --- Payments (funding-integrated; allocation across a single vendor's bills) -----------------

def open_bills_for(*, person=None, organization=None):
    """A vendor's open (unpaid / part-paid) bills, soonest-due first."""
    qs = Bill.objects.filter(status__in=_OPEN)
    qs = (
        qs.filter(vendor_person=person) if person
        else qs.filter(vendor_organization=organization)
    )
    return qs.order_by("due_date", "bill_date")


def apply_payment(payment, allocations, *, user=None):
    """Post a payment and record its allocations. Funding creates a native transaction in the
    owning module (bank withdrawal / card charge) so its register stays truthful; cash posts
    DR Accounts Payable / CR cash directly. `allocations` is a list of (bill, amount)."""
    from apps.banking.models import BankTransaction
    from apps.banking.models import TxnType as BankTxnType
    from apps.banking.services import post_transaction as bank_post
    from apps.cards.models import CardTxnType, CreditCardTransaction
    from apps.cards.services import post_transaction as card_post

    ap = resolve_account("accounts_payable")
    if payment.funding_kind == Payment.Funding.BANK and payment.bank_account_id:
        txn = BankTransaction.objects.create(
            account=payment.bank_account, txn_type=BankTxnType.WITHDRAWAL,
            date=payment.date, amount=payment.amount, category_account=ap,
            payee_person=payment.vendor_person, payee_organization=payment.vendor_organization,
        )
        bank_post(txn, user=user)
        payment.bank_txn = txn
    elif payment.funding_kind == Payment.Funding.CARD and payment.credit_card_id:
        txn = CreditCardTransaction.objects.create(
            card=payment.credit_card, txn_type=CardTxnType.CHARGE,
            date=payment.date, amount=payment.amount, category_account=ap,
            payee_person=payment.vendor_person, payee_organization=payment.vendor_organization,
        )
        card_post(txn, user=user)
        payment.card_txn = txn
    else:
        cash = payment.cash_account or resolve_account("1110")
        entry = post_entry(
            date=payment.date,
            lines=[
                LineInput(account=ap, debit=payment.amount,
                          person=payment.vendor_person, organization=payment.vendor_organization),
                LineInput(account=cash, credit=payment.amount),
            ],
            description=f"{payment.payment_number} — {payment.vendor_name}",
            source=payment,
            external_key=f"payables:payment:{payment.pk}:v{payment.posting_version}",
            user=user,
        )
        payment.journal_entry = entry
    payment.save()
    for bill, amt in allocations:
        if amt and amt > ZERO:
            PaymentAllocation.objects.create(payment=payment, bill=bill, amount=amt)
            recompute_bill_status(bill)
    return payment


def unapply_payment(payment, *, user=None):
    """Undo a payment (on delete): reverse the native funding transaction / cash entry and drop its
    allocations, then refresh the affected bills' status."""
    from apps.banking.services import unpost_transaction as bank_unpost
    from apps.cards.services import unpost_transaction as card_unpost

    bills = [a.bill for a in payment.allocations.all()]
    payment.allocations.all().delete()
    if payment.bank_txn_id:
        bank_unpost(payment.bank_txn)
        payment.bank_txn.delete()
    elif payment.card_txn_id:
        card_unpost(payment.card_txn)
        payment.card_txn.delete()
    elif payment.journal_entry_id and payment.journal_entry.status == JournalEntry.Status.POSTED:
        reverse_entry(payment.journal_entry, user=user)
    for bill in bills:
        recompute_bill_status(bill)


# --- Read models: aging, feeds, dashboard -----------------------------------------------------

def aging(as_of=None) -> dict:
    """Open payables bucketed by how overdue each bill is (current / 1-30 / 31-60 / 61-90 / 90+)."""
    as_of = as_of or datetime.date.today()
    buckets = {"current": ZERO, "d1_30": ZERO, "d31_60": ZERO, "d61_90": ZERO, "d90_plus": ZERO}
    for bill in Bill.objects.filter(status__in=_OPEN):
        bal = bill.balance_due
        if bal <= ZERO:
            continue
        days = (as_of - (bill.due_date or bill.bill_date)).days
        if days <= 0:
            buckets["current"] += bal
        elif days <= 30:
            buckets["d1_30"] += bal
        elif days <= 60:
            buckets["d31_60"] += bal
        elif days <= 90:
            buckets["d61_90"] += bal
        else:
            buckets["d90_plus"] += bal
    return buckets


def due_soon(within_days: int = 14):
    """Open bills due on/before `today + within_days` (past-due included), soonest first."""
    horizon = datetime.date.today() + datetime.timedelta(days=within_days)
    return Bill.objects.filter(
        status__in=_OPEN, due_date__isnull=False, due_date__lte=horizon
    ).order_by("due_date")


def warranty_expiring(within_days: int = 90):
    """Active tracked assets whose warranty ends on/before `today + within_days`, soonest first."""
    horizon = datetime.date.today() + datetime.timedelta(days=within_days)
    return AssetItem.objects.filter(
        status=AssetItem.Status.ACTIVE, warranty_end__isnull=False, warranty_end__lte=horizon
    ).order_by("warranty_end")


def dashboard_stats() -> dict:
    today = datetime.date.today()
    week = today + datetime.timedelta(days=7)
    open_bills = list(Bill.objects.filter(status__in=_OPEN))
    total_payable = sum((b.balance_due for b in open_bills), ZERO)
    overdue_total = sum(
        (b.balance_due for b in open_bills if b.due_date and b.due_date < today), ZERO
    )
    due_week = sum(
        (b.balance_due for b in open_bills if b.due_date and today <= b.due_date <= week), ZERO
    )
    return {
        "total_payable": total_payable,
        "open_count": len(open_bills),
        "overdue_total": overdue_total,
        "due_week": due_week,
        "vendor_count": VendorProfile.objects.count(),
    }
