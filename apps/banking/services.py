"""Banking service layer — the bridge from bank transactions to the general ledger.

Every `BankTransaction` becomes a balanced journal entry posted through `apps.finance.services`
(never a hand-written ledger row). The double-entry mapping per transaction type lives in
`_lines_for`. Posted entries are immutable, so an edit is a reverse-and-repost (bumping
`posting_version` so the idempotency `external_key` stays unique) and a delete is a reverse.

Each bank account owns one postable ledger account (`ensure_gl_account`) nested under the
`1120 Checking` / `1130 Savings` header, so its balance is just `account_balance(gl_account)`.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from apps.banking.models import AccountType as BankAccountType
from apps.banking.models import BankAccount, BankTransaction, TxnType
from apps.finance.models import ZERO, Account, AccountType, JournalEntry, Side
from apps.finance.services import (
    LineInput,
    post_entry,
    resolve_account,
    resolve_posting_account,
    reverse_entry,
)

# Fixed contra accounts (resolved by stable system_key / code). These are the Standard-mode
# defaults; in Expert mode a per-account PostingMap can override the *category* activities below
# (structural legs — opening equity, transfer clearing — are never remappable).
INTEREST_INCOME = "4400"          # Interest & Dividends
BANK_CHARGES = "bank_charges"     # 5850 Bank Charges (system_key)
TRANSFER_CLEARING = "transfer_clearing"  # 1150 Inter-account Transfer (system_key)
OPENING_EQUITY = "opening_balance_equity"  # 3100 (system_key)
DEFAULT_INCOME = "4900"           # Other Income
DEFAULT_EXPENSE = "5900"          # Other Expenses

# Category activities the Expert-mode Accounting Setup tab can remap, per bank account.
# `kind` selects which account list the picker offers (income = revenue, expense = expense).
POSTING_ACTIVITIES = [
    {"key": "deposit_income", "label": "Deposits", "kind": "income", "default": DEFAULT_INCOME},
    {"key": "withdrawal_expense", "label": "Withdrawals", "kind": "expense",
     "default": DEFAULT_EXPENSE},
    {"key": "interest_income", "label": "Interest earned", "kind": "income",
     "default": INTEREST_INCOME},
    {"key": "fee_expense", "label": "Fees & charges", "kind": "expense", "default": BANK_CHARGES},
]


# --- GL account provisioning ----------------------------------------------------------------

def _gl_name(account: BankAccount) -> str:
    masked = account.masked_number
    return f"{account.nickname} {masked}".strip() if masked else account.nickname


def _parent_code(account: BankAccount) -> str:
    return {
        BankAccountType.CHECKING: "1120",
        BankAccountType.SAVINGS: "1130",
        BankAccountType.CD: "1140",
    }.get(account.account_type, "1130")


def _next_child_code(parent: Account) -> str:
    """The next free `<parent.code>.NN` code (string-sorts after the header, before the sibling)."""
    prefix = f"{parent.code}."
    highest = 0
    for code in Account.objects.filter(parent=parent).values_list("code", flat=True):
        if code.startswith(prefix):
            suffix = code[len(prefix):]
            if suffix.isdigit():
                highest = max(highest, int(suffix))
    return f"{parent.code}.{highest + 1:02d}"


def ensure_gl_account(account: BankAccount, *, parent=None, existing=None) -> Account:
    """Create (or refresh) the postable ledger account that carries this bank account's balance.

    Standard mode auto-creates a child under the `1120`/`1130` header. Expert mode may pass a
    different `parent` header, or an `existing` postable account to adopt as this account's node."""
    if account.gl_account_id:
        gl = account.gl_account
        changed = []
        name = _gl_name(account)
        if gl.name != name:
            gl.name = name
            changed.append("name")
        # Currency is a ledger-tagging concern; only re-tag while the account has no postings.
        if gl.currency_id != account.currency_id and not gl.lines.exists():
            gl.currency = account.currency
            changed.append("currency")
        if changed:
            gl.save(update_fields=[*changed, "updated_at"])
        return gl

    if existing is not None:
        # Expert: adopt a pre-existing postable account as this bank account's ledger node.
        account.gl_account = existing
        account.save(update_fields=["gl_account"])
        return existing

    parent = parent or resolve_account(_parent_code(account))
    gl = Account.objects.create(
        code=_next_child_code(parent),
        name=_gl_name(account),
        type=AccountType.ASSET,
        normal_side=Side.DEBIT,
        currency=account.currency,
        parent=parent,
        is_postable=True,
        is_system=False,
    )
    account.gl_account = gl
    account.save(update_fields=["gl_account"])
    return gl


# --- Posting ---------------------------------------------------------------------------------

def _external_key(txn: BankTransaction) -> str:
    return f"banking:txn:{txn.pk}:v{txn.posting_version}"


def _description(txn: BankTransaction) -> str:
    return f"{txn.account.nickname}: {txn.type_label}"


def _lines_for(txn: BankTransaction) -> list[LineInput]:
    """The balanced debit/credit pair(s) for a transaction (see the module docstring's mapping)."""
    gl = ensure_gl_account(txn.account)
    amount = txn.amount
    cur = txn.account.currency
    payee = {"person": txn.payee_person, "organization": txn.payee_organization}
    bank = {"organization": txn.account.bank}

    def line(account, *, debit=ZERO, credit=ZERO, **party):
        return LineInput(account, debit=debit, credit=credit, currency=cur, **party)

    acct = txn.account  # the PostingMap owner for Expert-mode category overrides

    t = txn.txn_type
    if t == TxnType.OPENING:
        return [line(gl, debit=amount), line(OPENING_EQUITY, credit=amount)]
    if t == TxnType.DEPOSIT:
        contra = txn.category_account or resolve_posting_account(
            acct, "deposit_income", DEFAULT_INCOME
        )
        return [line(gl, debit=amount), line(contra, credit=amount, **payee)]
    if t == TxnType.WITHDRAWAL:
        contra = txn.category_account or resolve_posting_account(
            acct, "withdrawal_expense", DEFAULT_EXPENSE
        )
        return [line(contra, debit=amount, **payee), line(gl, credit=amount)]
    if t == TxnType.INTEREST:
        contra = resolve_posting_account(acct, "interest_income", INTEREST_INCOME)
        return [line(gl, debit=amount), line(contra, credit=amount, **bank)]
    if t in (TxnType.FEE, TxnType.CHARGE):
        contra = resolve_posting_account(acct, "fee_expense", BANK_CHARGES)
        return [line(contra, debit=amount, **bank), line(gl, credit=amount)]
    if t == TxnType.TRANSFER_OUT:
        return [line(TRANSFER_CLEARING, debit=amount), line(gl, credit=amount)]
    if t == TxnType.TRANSFER_IN:
        return [line(gl, debit=amount), line(TRANSFER_CLEARING, credit=amount)]
    raise ValueError(f"Unknown transaction type {t!r}")


def post_transaction(txn: BankTransaction, *, user=None) -> JournalEntry:
    """Post a saved transaction to the ledger and link the resulting entry back onto it."""
    entry_type = (
        JournalEntry.EntryType.OPENING
        if txn.txn_type == TxnType.OPENING
        else JournalEntry.EntryType.STANDARD
    )
    entry = post_entry(
        date=txn.date,
        lines=_lines_for(txn),
        entry_type=entry_type,
        currency=txn.account.currency,
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


def repost_transaction(txn: BankTransaction, *, user=None) -> JournalEntry:
    """Reverse the current entry and post a fresh one (edit path; posted entries are immutable)."""
    current = txn.journal_entry
    if current is not None and current.status == JournalEntry.Status.POSTED:
        reverse_entry(current, user=user)
    txn.posting_version += 1
    txn.save(update_fields=["posting_version", "updated_at"])
    return post_transaction(txn, user=user)


def unpost_transaction(txn: BankTransaction, *, user=None) -> None:
    """Reverse the transaction's entry (used when a transaction is deleted); balances net out."""
    current = txn.journal_entry
    if current is not None and current.status == JournalEntry.Status.POSTED:
        reverse_entry(current, user=user)


def delete_transaction(txn: BankTransaction, *, user=None) -> None:
    """Hard-remove the transaction and its GL entry — used to erase a data-entry mistake (vs
    `unpost_transaction`, which reverses). The entry's lines cascade and the row is truly gone.
    Scoped to non-transfer transactions (no counter leg to unwind)."""
    entry = txn.journal_entry
    if entry is not None:
        entry.hard_delete()
    txn.hard_delete()


def create_matching_leg(txn: BankTransaction, *, user=None) -> BankTransaction | None:
    """For a transfer against a tracked counter account, create + post the opposite leg."""
    if txn.counter_account_id is None or txn.txn_type not in (
        TxnType.TRANSFER_OUT,
        TxnType.TRANSFER_IN,
    ):
        return None
    opposite = (
        TxnType.TRANSFER_IN if txn.txn_type == TxnType.TRANSFER_OUT else TxnType.TRANSFER_OUT
    )
    leg = BankTransaction.objects.create(
        account=txn.counter_account,
        txn_type=opposite,
        date=txn.date,
        amount=txn.amount,
        counter_account=txn.account,
        memo=txn.memo,
        reference=txn.reference,
    )
    post_transaction(leg, user=user)
    return leg


# --- Holder ↔ organization ("Account Holder" P2O) synchronisation ----------------------------

def sync_holder_p2o(account: BankAccount, *, user=None) -> None:
    """Ensure each current holder has an org-level 'Account Holder' link to the bank. Add-only —
    never removes edges (they may be managed by hand in the relationships graph)."""
    from apps.relationships.models import PersonOrgRelationship, PersonOrgRelationshipType

    rtype = PersonOrgRelationshipType.objects.filter(code="account_holder").first()
    if rtype is None:
        return
    for holder in account.holders.all():
        PersonOrgRelationship.objects.get_or_create(
            person=holder.person, organization=account.bank, type=rtype
        )


# --- Read models -----------------------------------------------------------------------------

REGISTER_PER_PAGE = 50


def register(account: BankAccount, *, page=1, per_page=REGISTER_PER_PAGE) -> dict:
    """One PAGE of the account register (newest first), each transaction with its chronological
    running balance (in the account's own currency). The balance is a window SUM over (date, id) —
    the balance AFTER each transaction — and sorting/pagination happen in the database, so only the
    page's rows are ever materialized no matter how large the register grows."""
    from django.core.paginator import Paginator
    from django.db.models import F, Sum, Window

    from apps.banking.models import signed_amount_sql

    txns = account.transactions.select_related(
        "category_account", "counter_account", "payee_person", "payee_organization",
    ).annotate(
        balance_after=Window(Sum(signed_amount_sql()), order_by=[F("date").asc(), F("id").asc()]),
    ).order_by("-date", "-id")
    page_obj = Paginator(txns, per_page).get_page(page)
    rows = [{"txn": t, "balance": t.balance_after} for t in page_obj.object_list]
    return {"rows": rows, "page_obj": page_obj, "total": page_obj.paginator.count}


def attach_balances(accounts) -> list[BankAccount]:
    """Stamp each account's base + native balance from the batch finance aggregates (three grouped
    queries TOTAL, however many accounts), so `a.balance` / `a.display_balance` in a loop or a
    template row no longer fire a subtree walk + full aggregate per account."""
    from apps.finance.services import account_balances, account_native_balances

    accounts = list(accounts)
    gl_ids = [a.gl_account_id for a in accounts if a.gl_account_id]
    gl_accounts = list(Account.objects.filter(pk__in=gl_ids))
    base = account_balances(gl_accounts)
    native = account_native_balances(gl_accounts)
    for a in accounts:
        a._balance = base.get(a.gl_account_id, ZERO)
        a._native_balance = native.get(a.gl_account_id, ZERO)
    return accounts


def total_balance() -> Decimal:
    """Sum of all bank-account balances, in the base currency."""
    return sum((a.balance for a in attach_balances(BankAccount.objects.all())), ZERO)


def dashboard_stats() -> dict:
    """Headline figures for the Banking dashboard."""
    today = datetime.date.today()
    month_start = today.replace(day=1)
    accounts = attach_balances(BankAccount.objects.select_related("bank"))
    return {
        "accounts_count": len(accounts),
        "banks_count": len({a.bank_id for a in accounts}),
        "cds_count": sum(1 for a in accounts if a.account_type == BankAccountType.CD),
        "total_balance": sum((a.balance for a in accounts), ZERO),
        "txns_this_month": BankTransaction.objects.filter(date__gte=month_start).count(),
        "accounts": accounts,
    }


def cd_maturities(within_days: int = 365) -> list[BankAccount]:
    """Bank CDs approaching (or past-due and still open) maturity within the window, soonest first.

    Mirrors investments.services.upcoming_maturities but for bank CD accounts. Excludes CDs already
    marked closed; past-due-but-open CDs (negative days_to_maturity) sort first as a nudge to act.
    """
    horizon = datetime.date.today() + datetime.timedelta(days=within_days)
    return list(
        BankAccount.objects.filter(
            account_type=BankAccountType.CD,
            maturity_date__isnull=False,
            maturity_date__lte=horizon,
            closed_year__isnull=True,
        )
        .select_related("bank")
        .order_by("maturity_date")
    )
