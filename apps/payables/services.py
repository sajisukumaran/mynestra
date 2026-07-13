"""Payables service layer — the ONLY sanctioned path to the general ledger, plus subledger helpers.

A bill posts accrual double-entry through `apps.finance.services` (never direct JournalEntry/Line
rows): each line DRs an expense/asset (shipping/tax to their own accounts, a discount CRs Purchase
Discounts) and the bill total CRs Accounts Payable, tagged with the vendor party. Bills post on save
and are edited **in place** via `finance.repost_entry` (no reversal); deleting reverses. Capitalized
lines materialize an `AssetItem` (held at cost, warranty-tracked).
"""

from decimal import Decimal

from apps.finance.models import JournalEntry
from apps.finance.services import (
    LineInput,
    post_entry,
    repost_entry,
    resolve_account,
    reverse_entry,
)
from apps.payables.models import AssetItem, Bill, BillLine, VendorProfile

ZERO = Decimal("0")

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
