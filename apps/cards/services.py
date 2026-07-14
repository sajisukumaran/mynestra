"""Cards service layer — the bridge from credit-card transactions to the general ledger.

Every `CreditCardTransaction` becomes a balanced journal entry posted through the finance service
(never a hand-written ledger row). The double-entry mapping per type lives in `_lines_for`. Posted
entries are immutable, so an edit is a reverse-and-repost (bumping `posting_version`) and a delete
is a reverse. Each credit card owns one postable LIABILITY ledger account (`ensure_gl_account`)
nested under the `2100 Credit Cards` header, so its balance owed is `account_balance(gl_account)`.

Category contras (charge/interest/fee/reward) resolve through `resolve_posting_account`, so an
Expert-mode per-card PostingMap can redirect them; the structural legs (opening equity, the 1150
clearing used by payments) are system-managed. Debit cards have no ledger — they're a registry — so
they have no posting logic here.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from apps.cards.models import CardTxnType, CreditCard, CreditCardTransaction, DebitCard
from apps.finance.models import ZERO, Account, AccountType, JournalEntry, Side
from apps.finance.services import (
    LineInput,
    post_entry,
    resolve_account,
    resolve_posting_account,
    reverse_entry,
)

# Contra accounts (Standard-mode defaults; the category ones are remappable per-card in Expert).
CARD_HEADER = "credit_cards"       # 2100 header (system_key) — cards nest beneath it
OPENING_EQUITY = "opening_balance_equity"  # 3100 (system_key)
TRANSFER_CLEARING = "transfer_clearing"    # 1150 (system_key) — payment clearing
INTEREST_EXPENSE = "interest_expense"      # 5860 (system_key)
CARD_FEES = "bank_charges"         # 5850 (system_key)
REWARD_INCOME = "4900"             # Other Income (statement credits / cashback)
DEFAULT_EXPENSE = "5900"           # Other Expenses (default charge category)

# Category activities the Expert-mode Accounting Setup tab can remap, per credit card.
POSTING_ACTIVITIES = [
    {"key": "charge_category", "label": "Charges (default category)", "kind": "expense",
     "default": DEFAULT_EXPENSE},
    {"key": "interest_expense", "label": "Interest", "kind": "expense",
     "default": INTEREST_EXPENSE},
    {"key": "fee_expense", "label": "Fees", "kind": "expense", "default": CARD_FEES},
    {"key": "reward_income", "label": "Statement credits", "kind": "income",
     "default": REWARD_INCOME},
]


# --- GL account provisioning ----------------------------------------------------------------

def _gl_name(card: CreditCard) -> str:
    masked = card.masked_number
    return f"{card.nickname} {masked}".strip() if masked else card.nickname


def _next_child_code(parent: Account) -> str:
    """The next free `<parent.code>.NN` code (e.g. 2100.01, 2100.02)."""
    prefix = f"{parent.code}."
    highest = 0
    for code in Account.objects.filter(parent=parent).values_list("code", flat=True):
        if code.startswith(prefix):
            suffix = code[len(prefix):]
            if suffix.isdigit():
                highest = max(highest, int(suffix))
    return f"{parent.code}.{highest + 1:02d}"


def ensure_gl_account(card: CreditCard, *, parent=None, existing=None) -> Account:
    """Create (or refresh) the postable LIABILITY ledger account carrying this card's balance owed.

    Standard mode nests a child under the `2100 Credit Cards` header. Expert mode may pass a
    different `parent` header, or an `existing` postable account to adopt as this card's node."""
    if card.gl_account_id:
        gl = card.gl_account
        changed = []
        name = _gl_name(card)
        if gl.name != name:
            gl.name = name
            changed.append("name")
        if gl.currency_id != card.currency_id and not gl.lines.exists():
            gl.currency = card.currency
            changed.append("currency")
        if changed:
            gl.save(update_fields=[*changed, "updated_at"])
        return gl

    if existing is not None:
        card.gl_account = existing
        card.save(update_fields=["gl_account"])
        return existing

    parent = parent or resolve_account(CARD_HEADER)
    gl = Account.objects.create(
        code=_next_child_code(parent),
        name=_gl_name(card),
        type=AccountType.LIABILITY,
        normal_side=Side.CREDIT,
        currency=card.currency,
        parent=parent,
        is_postable=True,
        is_system=False,
    )
    card.gl_account = gl
    card.save(update_fields=["gl_account"])
    return gl


# --- Posting ---------------------------------------------------------------------------------

def _external_key(txn: CreditCardTransaction) -> str:
    return f"cards:txn:{txn.pk}:v{txn.posting_version}"


def _description(txn: CreditCardTransaction) -> str:
    return f"{txn.card.nickname}: {txn.type_label}"


def _lines_for(txn: CreditCardTransaction) -> list[LineInput]:
    """The balanced debit/credit pair for a transaction. gl = the card's liability account:
    charges/interest/fees CREDIT it (owe more); payments/refunds/credits DEBIT it (owe less)."""
    gl = ensure_gl_account(txn.card)
    amount = txn.amount
    cur = txn.card.currency
    payee = {"person": txn.payee_person, "organization": txn.payee_organization}
    issuer = {"organization": txn.card.issuer}
    card = txn.card

    def line(account, *, debit=ZERO, credit=ZERO, **party):
        return LineInput(account, debit=debit, credit=credit, currency=cur, **party)

    t = txn.txn_type
    if t == CardTxnType.OPENING:
        return [line(OPENING_EQUITY, debit=amount), line(gl, credit=amount)]
    if t == CardTxnType.CHARGE:
        contra = txn.category_account or resolve_posting_account(
            card, "charge_category", DEFAULT_EXPENSE
        )
        return [line(contra, debit=amount, **payee), line(gl, credit=amount)]
    if t == CardTxnType.PAYMENT:
        return [line(gl, debit=amount), line(TRANSFER_CLEARING, credit=amount)]
    if t == CardTxnType.INTEREST:
        contra = resolve_posting_account(card, "interest_expense", INTEREST_EXPENSE)
        return [line(contra, debit=amount, **issuer), line(gl, credit=amount)]
    if t == CardTxnType.FEE:
        contra = resolve_posting_account(card, "fee_expense", CARD_FEES)
        return [line(contra, debit=amount, **issuer), line(gl, credit=amount)]
    if t == CardTxnType.REFUND:
        contra = txn.category_account or resolve_posting_account(
            card, "charge_category", DEFAULT_EXPENSE
        )
        return [line(gl, debit=amount), line(contra, credit=amount, **payee)]
    if t == CardTxnType.CREDIT:
        contra = resolve_posting_account(card, "reward_income", REWARD_INCOME)
        return [line(gl, debit=amount), line(contra, credit=amount, **issuer)]
    raise ValueError(f"Unknown card transaction type {t!r}")


def post_transaction(txn: CreditCardTransaction, *, user=None) -> JournalEntry:
    """Post a saved transaction to the ledger and link the resulting entry back onto it."""
    entry_type = (
        JournalEntry.EntryType.OPENING
        if txn.txn_type == CardTxnType.OPENING
        else JournalEntry.EntryType.STANDARD
    )
    entry = post_entry(
        date=txn.date,
        lines=_lines_for(txn),
        entry_type=entry_type,
        currency=txn.card.currency,
        source=txn,
        external_key=_external_key(txn),
        description=_description(txn),
        memo=txn.memo,
        reference=txn.reference,
        user=user,
    )
    if txn.journal_entry_id != entry.pk:
        txn.journal_entry = entry
        txn.save(update_fields=["journal_entry", "updated_at"])
    return entry


def repost_transaction(txn: CreditCardTransaction, *, user=None) -> JournalEntry:
    """Reverse the current entry and post a fresh one (edit path; posted entries are immutable)."""
    current = txn.journal_entry
    if current is not None and current.status == JournalEntry.Status.POSTED:
        reverse_entry(current, user=user)
    txn.posting_version += 1
    txn.save(update_fields=["posting_version", "updated_at"])
    return post_transaction(txn, user=user)


def unpost_transaction(txn: CreditCardTransaction, *, user=None) -> None:
    """Reverse the transaction's entry (used when a transaction is deleted); balances net out."""
    current = txn.journal_entry
    if current is not None and current.status == JournalEntry.Status.POSTED:
        reverse_entry(current, user=user)


def delete_transaction(txn: CreditCardTransaction, *, user=None) -> None:
    """Hard-remove the transaction and its GL entry — used to erase a data-entry mistake (vs
    `unpost_transaction`, which reverses). The entry's lines cascade and the row is truly gone."""
    entry = txn.journal_entry
    if entry is not None:
        entry.hard_delete()
    txn.hard_delete()


def create_matching_leg(txn: CreditCardTransaction, *, user=None):
    """For a payment from a tracked bank account, create + post the matching bank withdrawal
    (a TRANSFER_OUT), so the 1150 clearing account nets to zero across both modules."""
    if txn.txn_type != CardTxnType.PAYMENT or txn.counter_account_id is None:
        return None
    from apps.banking.models import BankTransaction
    from apps.banking.models import TxnType as BankTxnType
    from apps.banking.services import post_transaction as post_bank_txn

    leg = BankTransaction.objects.create(
        account=txn.counter_account,
        txn_type=BankTxnType.TRANSFER_OUT,
        date=txn.date,
        amount=txn.amount,
        counter_external=f"{txn.card.nickname} payment",
        memo=txn.memo,
        reference=txn.reference,
    )
    post_bank_txn(leg, user=user)
    return leg


# --- Holder ↔ issuer ("Cardholder" P2O) synchronisation --------------------------------------

def sync_holder_p2o(card: CreditCard, *, user=None) -> None:
    """Ensure each current holder has an org-level 'Cardholder' link to the issuer. Add-only."""
    from apps.relationships.models import PersonOrgRelationship, PersonOrgRelationshipType

    rtype = PersonOrgRelationshipType.objects.filter(code="cardholder").first()
    if rtype is None:
        return
    for holder in card.holders.all():
        PersonOrgRelationship.objects.get_or_create(
            person=holder.person, organization=card.issuer, type=rtype
        )


# --- Read models -----------------------------------------------------------------------------

REGISTER_PER_PAGE = 50


def register(card: CreditCard, *, page=1, per_page=REGISTER_PER_PAGE) -> dict:
    """One PAGE of the card register (newest first), each transaction with its chronological
    running balance owed. The balance is a window SUM over (date, id) — the balance AFTER each
    transaction — and sorting/pagination happen in the database, so only the page's rows are ever
    materialized no matter how large the register grows."""
    from django.core.paginator import Paginator
    from django.db.models import F, Sum, Window

    from apps.cards.models import signed_amount_sql

    txns = card.transactions.select_related(
        "category_account", "counter_account", "payee_person", "payee_organization",
    ).annotate(
        balance_after=Window(Sum(signed_amount_sql()), order_by=[F("date").asc(), F("id").asc()]),
    ).order_by("-date", "-id")
    page_obj = Paginator(txns, per_page).get_page(page)
    rows = [{"txn": t, "balance": t.balance_after} for t in page_obj.object_list]
    return {"rows": rows, "page_obj": page_obj, "total": page_obj.paginator.count}


def attach_balances(cards) -> list[CreditCard]:
    """Stamp each card's base + native balance owed from the batch finance aggregates (three
    grouped queries TOTAL, however many cards), so `c.balance` / `c.display_balance` /
    `c.utilization` in a loop or a template row no longer fire an aggregate per card."""
    from apps.finance.services import account_balances, account_native_balances

    cards = list(cards)
    gl_ids = [c.gl_account_id for c in cards if c.gl_account_id]
    gl_accounts = list(Account.objects.filter(pk__in=gl_ids))
    base = account_balances(gl_accounts)
    native = account_native_balances(gl_accounts)
    for c in cards:
        c._balance = base.get(c.gl_account_id, ZERO)
        c._native_balance = native.get(c.gl_account_id, ZERO)
    return cards


def total_owed() -> Decimal:
    """Sum of all credit-card balances owed, in the base currency."""
    return sum((c.balance for c in attach_balances(CreditCard.objects.all())), ZERO)


def dashboard_stats() -> dict:
    """Headline figures for the Cards dashboard."""
    today = datetime.date.today()
    month_start = today.replace(day=1)
    credit_cards = attach_balances(CreditCard.objects.select_related("issuer"))
    return {
        "credit_count": len(credit_cards),
        "debit_count": DebitCard.objects.count(),
        "total_owed": sum((c.balance for c in credit_cards), ZERO),
        "txns_this_month": CreditCardTransaction.objects.filter(date__gte=month_start).count(),
        "credit_cards": credit_cards,
    }
