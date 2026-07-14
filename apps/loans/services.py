"""Loans service layer — the bridge from loan transactions to the general ledger.

Every `LoanTransaction` becomes a balanced journal entry posted through `apps.finance.services`
(never a hand-written ledger row); the per-type mapping lives in `_lines_for`. Posted entries are
immutable, so an edit is a reverse-and-repost (bumping `posting_version`), rebuilding the matched
bank leg. Each loan owns one postable liability node (`ensure_gl_account`) nested under
the header matching its `loan_type` (or `2950 Contingent Liabilities` when it's off net worth), so
its balance owed is just `account_balance(gl_account)`.

The one twist over Cards/Banking: a payment carries a component split. Only principal + extra reduce
the liability; interest → `5860`, escrow → `5140`, fees → `5850` are expenses. A payment funded from
an untracked source / another party (the co-signer's son) posts ONLY the principal reduction against
opening equity, tagged with the payer — interest/escrow/fees are recorded on the row but not booked,
so net worth rises correctly and the P&L is not distorted.
"""

from __future__ import annotations

import datetime
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
from apps.loans.models import (
    LOAN_TYPE_PARENT,
    PERIODS_PER_YEAR,
    SPLIT_TYPES,
    Funding,
    Loan,
    LoanTransaction,
    LoanTxnType,
)

# Fixed contra accounts (resolved by stable system_key / code). Category activities are remappable
# per loan in Expert mode; structural legs (opening equity, clearing, cash) never are.
INTEREST_EXPENSE = "interest_expense"      # 5860
ESCROW_DEFAULT = "property_tax"            # 5140 (Expert can remap to 5150 Home Insurance)
FEE_EXPENSE = "bank_charges"               # 5850
TRANSFER_CLEARING = "transfer_clearing"    # 1150
OPENING_EQUITY = "opening_balance_equity"  # 3100
CASH_ON_HAND = "1110"
CONTINGENT_HEADER = "2950"                 # Contingent Liabilities (off net worth)

# Category activities the Expert-mode Accounting tab can remap, per loan.
POSTING_ACTIVITIES = [
    {"key": "interest_expense", "label": "Interest", "kind": "expense",
     "default": INTEREST_EXPENSE},
    {"key": "escrow", "label": "Escrow (tax / insurance)", "kind": "expense",
     "default": ESCROW_DEFAULT},
    {"key": "fee_expense", "label": "Fees & penalties", "kind": "expense", "default": FEE_EXPENSE},
]


# --- GL account provisioning ----------------------------------------------------------------

def _gl_name(loan: Loan) -> str:
    return loan.nickname


def _parent_code(loan: Loan) -> str:
    """The COA header this loan's node nests under: 2950 when it's off net worth, else the
    loan_type header (2210..2900)."""
    if not loan.counts_toward_net_worth:
        return CONTINGENT_HEADER
    return LOAN_TYPE_PARENT.get(loan.loan_type, "2900")


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


def ensure_gl_account(loan: Loan, *, parent=None, existing=None) -> Account:
    """Create (or reconcile) the postable liability node carrying this loan's balance owed.

    Standard mode auto-creates a child under the type/contingent header. Expert mode may pass a
    different `parent` header or an `existing` postable account to adopt. On a refresh it also
    reconciles the parent — flipping `counts_toward_net_worth` (or changing loan_type) re-parents +
    re-codes the node (rare; nodes are is_system=False so the move is allowed)."""
    if loan.gl_account_id:
        gl = loan.gl_account
        changed = []
        name = _gl_name(loan)
        if gl.name != name:
            gl.name = name
            changed.append("name")
        if parent is None:  # only auto-reconcile the parent in Standard provisioning
            desired = _parent_code(loan)
            if gl.parent is None or gl.parent.code != desired:
                new_parent = resolve_account(desired)
                gl.parent = new_parent
                gl.code = _next_child_code(new_parent)
                changed += ["parent", "code"]
        if gl.currency_id != loan.currency_id and not gl.lines.exists():
            gl.currency = loan.currency
            changed.append("currency")
        if changed:
            gl.save(update_fields=[*changed, "updated_at"])
        return gl

    if existing is not None:
        loan.gl_account = existing
        loan.save(update_fields=["gl_account"])
        return existing

    parent = parent or resolve_account(_parent_code(loan))
    gl = Account.objects.create(
        code=_next_child_code(parent),
        name=_gl_name(loan),
        type=AccountType.LIABILITY,
        normal_side=Side.CREDIT,
        currency=loan.currency,
        parent=parent,
        is_postable=True,
        is_system=False,
    )
    loan.gl_account = gl
    loan.save(update_fields=["gl_account"])
    return gl


# --- Posting ---------------------------------------------------------------------------------

def _external_key(txn: LoanTransaction) -> str:
    return f"loans:txn:{txn.pk}:v{txn.posting_version}"


def _description(txn: LoanTransaction) -> str:
    return f"{txn.loan.nickname}: {txn.type_label}"


def _cash_account(txn: LoanTransaction):
    return txn.cash_account or resolve_account(CASH_ON_HAND)


def _payment_lines(txn, gl, line, lender, payer):
    """A payment / extra-principal entry. Bank/cash fund the full split; an external payment posts
    only the principal reduction against opening equity (tagged with the payer)."""
    loan = txn.loan
    reduction = txn.principal_reduction  # principal + extra_principal
    if txn.funding_source == Funding.EXTERNAL:
        if reduction <= ZERO:
            return []  # an all-interest external payment has no effect on the tracked ledger
        return [
            line(gl, debit=reduction, **lender),
            line(OPENING_EQUITY, credit=reduction, **payer),
        ]

    lines = []
    if reduction > ZERO:
        lines.append(line(gl, debit=reduction, **lender))
    if txn.interest > ZERO:
        lines.append(
            line(
                resolve_posting_account(loan, "interest_expense", INTEREST_EXPENSE),
                debit=txn.interest,
                **lender,
            )
        )
    if txn.escrow > ZERO:
        lines.append(
            line(resolve_posting_account(loan, "escrow", ESCROW_DEFAULT), debit=txn.escrow)
        )
    if txn.fees > ZERO:
        lines.append(
            line(
                resolve_posting_account(loan, "fee_expense", FEE_EXPENSE),
                debit=txn.fees,
                **lender,
            )
        )
    total = reduction + txn.interest + txn.escrow + txn.fees  # == txn.amount
    credit_account = _cash_account(txn) if txn.funding_source == Funding.CASH else TRANSFER_CLEARING
    lines.append(line(credit_account, credit=total))
    return lines


def _lines_for(txn: LoanTransaction) -> list[LineInput]:
    """The balanced lines for a transaction — the posting matrix (see the module docstring)."""
    loan = txn.loan
    gl = ensure_gl_account(loan)
    cur = loan.currency
    lender = {"person": loan.lender_person, "organization": loan.lender_organization}
    payer = {"person": txn.payer_person, "organization": txn.payer_organization}
    amount = txn.amount

    def line(account, *, debit=ZERO, credit=ZERO, **party):
        return LineInput(account, debit=debit, credit=credit, currency=cur, **party)

    t = txn.txn_type
    funded_from_bank = txn.funding_source == Funding.BANK and txn.funding_account_id

    if t == LoanTxnType.OPENING:
        return [line(OPENING_EQUITY, debit=amount), line(gl, credit=amount, **lender)]
    if t in (LoanTxnType.DISBURSEMENT, LoanTxnType.DRAW):
        if funded_from_bank:
            debit_leg = line(TRANSFER_CLEARING, debit=amount)
        elif txn.funding_source == Funding.CASH:
            debit_leg = line(_cash_account(txn), debit=amount)
        else:
            debit_leg = line(OPENING_EQUITY, debit=amount)
        return [debit_leg, line(gl, credit=amount, **lender)]
    if t == LoanTxnType.INTEREST:
        contra = resolve_posting_account(loan, "interest_expense", INTEREST_EXPENSE)
        return [line(contra, debit=amount, **lender), line(gl, credit=amount, **lender)]
    if t == LoanTxnType.FEE:
        contra = resolve_posting_account(loan, "fee_expense", FEE_EXPENSE)
        return [line(contra, debit=amount, **lender), line(gl, credit=amount, **lender)]
    if t == LoanTxnType.ADJUSTMENT:
        if txn.increase:
            return [line(OPENING_EQUITY, debit=amount), line(gl, credit=amount, **lender)]
        return [line(gl, debit=amount, **lender), line(OPENING_EQUITY, credit=amount)]
    if t in SPLIT_TYPES:  # PAYMENT / EXTRA_PRINCIPAL
        return _payment_lines(txn, gl, line, lender, payer)
    raise ValueError(f"Unknown loan transaction type {t!r}")


def post_transaction(txn: LoanTransaction, *, user=None) -> JournalEntry | None:
    """Post a saved transaction to the ledger and link the entry back onto it. Returns None when the
    transaction has no ledger effect (an external all-interest payment)."""
    lines = _lines_for(txn)
    if len(lines) < 2:
        if txn.journal_entry_id is not None:
            txn.journal_entry = None
            txn.save(update_fields=["journal_entry", "updated_at"])
        return None
    entry_type = (
        JournalEntry.EntryType.OPENING
        if txn.txn_type == LoanTxnType.OPENING
        else JournalEntry.EntryType.STANDARD
    )
    entry = post_entry(
        date=txn.date,
        lines=lines,
        entry_type=entry_type,
        currency=txn.loan.currency,
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


def _teardown_bank_leg(txn: LoanTransaction, *, user=None) -> None:
    """Hard-remove the matched bank leg (and its GL entry) this transaction created, if any."""
    if txn.bank_txn_id is None:
        return
    from apps.banking.services import delete_transaction as delete_bank_txn

    leg = txn.bank_txn
    txn.bank_txn = None
    txn.save(update_fields=["bank_txn", "updated_at"])
    delete_bank_txn(leg, user=user)


@transaction.atomic
def repost_transaction(txn: LoanTransaction, *, user=None) -> JournalEntry | None:
    """Reverse the current entry, rebuild the matched leg, and post a fresh entry (edit path)."""
    _teardown_bank_leg(txn, user=user)
    current = txn.journal_entry
    if current is not None and current.status == JournalEntry.Status.POSTED:
        reverse_entry(current, user=user)
    txn.posting_version += 1
    txn.save(update_fields=["posting_version", "updated_at"])
    entry = post_transaction(txn, user=user)
    create_matching_leg(txn, user=user)
    return entry


def unpost_transaction(txn: LoanTransaction, *, user=None) -> None:
    """Reverse the transaction's entry (Void — keeps the record); also reverses the matched leg."""
    current = txn.journal_entry
    if current is not None and current.status == JournalEntry.Status.POSTED:
        reverse_entry(current, user=user)
    if txn.bank_txn_id is not None:
        from apps.banking.services import unpost_transaction as unpost_bank_txn

        unpost_bank_txn(txn.bank_txn, user=user)


@transaction.atomic
def delete_transaction(txn: LoanTransaction, *, user=None) -> None:
    """Hard-erase the transaction, its GL entry, and its matched bank leg — the mistake-eraser (vs
    `unpost_transaction`, which reverses and keeps the record)."""
    _teardown_bank_leg(txn, user=user)
    entry = txn.journal_entry
    if entry is not None:
        entry.hard_delete()
    txn.hard_delete()


@transaction.atomic
def create_matching_leg(txn: LoanTransaction, *, user=None):
    """For a payment/disbursement/draw funded through a tracked bank account, create + post the
    matching bank transaction (a withdrawal for money leaving, a deposit-in for money arriving), so
    the 1150 clearing account nets to zero across both modules."""
    if txn.funding_source != Funding.BANK or txn.funding_account_id is None:
        return None
    from apps.banking.models import BankTransaction
    from apps.banking.models import TxnType as BankTxnType
    from apps.banking.services import post_transaction as post_bank_txn

    if txn.txn_type in SPLIT_TYPES:  # money leaves the funding account
        bank_type = BankTxnType.TRANSFER_OUT
    elif txn.txn_type in (LoanTxnType.DISBURSEMENT, LoanTxnType.DRAW):  # money arrives
        bank_type = BankTxnType.TRANSFER_IN
    else:
        return None

    leg = BankTransaction.objects.create(
        account=txn.funding_account,
        txn_type=bank_type,
        date=txn.date,
        amount=txn.amount,
        counter_external=f"{txn.loan.nickname} {txn.type_label.lower()}",
        memo=txn.memo,
        reference=txn.reference,
    )
    post_bank_txn(leg, user=user)
    txn.bank_txn = leg
    txn.save(update_fields=["bank_txn", "updated_at"])
    return leg


# --- Borrower ↔ lender ("Borrower" P2O) synchronisation --------------------------------------

def sync_borrower_p2o(loan: Loan, *, user=None) -> None:
    """Ensure each borrower has an org-level 'Borrower' link to an organization lender. Add-only;
    no-ops when the loan's lender is a person or the P2O type isn't seeded."""
    if loan.lender_organization_id is None:
        return
    from apps.relationships.models import PersonOrgRelationship, PersonOrgRelationshipType

    rtype = PersonOrgRelationshipType.objects.filter(code="borrower").first()
    if rtype is None:
        return
    for borrower in loan.borrowers.all():
        PersonOrgRelationship.objects.get_or_create(
            person=borrower.person, organization=loan.lender_organization, type=rtype
        )


# --- Read models -----------------------------------------------------------------------------

def rate_on(loan: Loan, on_date: datetime.date):
    """The APR (percent) in effect on `on_date`: the latest LoanRateChange on/before it, else the
    origination `annual_rate`."""
    change = (
        loan.rate_changes.filter(effective_date__lte=on_date)
        .order_by("-effective_date")
        .first()
    )
    return change.annual_rate if change else loan.annual_rate


def register(loan: Loan) -> list[dict]:
    """The full register (newest first), each transaction with its chronological running balance."""
    txns = list(
        loan.transactions.select_related("payer_person", "payer_organization", "funding_account")
        .order_by("date", "id")
    )
    running = ZERO
    rows = []
    for txn in txns:
        running += txn.balance_delta
        rows.append({"txn": txn, "balance": running})
    rows.reverse()
    return rows


def _party_name(party) -> str:
    if party is None:
        return ""
    for attr in ("display_name", "full_name", "name"):
        val = getattr(party, attr, "")
        if val:
            return val
    return str(party)


def contributions_by_borrower(loan: Loan) -> list[dict]:
    """Principal (+ extra) paid down, grouped by who paid: an external payment → its payer; a
    bank/cash-funded payment → 'You / household'. Yields 'you paid $X, your son $Y'."""
    buckets: dict = {}
    for txn in loan.transactions.filter(txn_type__in=SPLIT_TYPES).select_related(
        "payer_person", "payer_organization"
    ):
        reduction = txn.principal_reduction
        if reduction <= ZERO:
            continue
        if txn.funding_source == Funding.EXTERNAL and txn.payer is not None:
            key = f"party:{txn.payer_person_id or ''}:{txn.payer_organization_id or ''}"
            label = _party_name(txn.payer)
        else:
            key = "household"
            label = "You / household"
        bucket = buckets.setdefault(key, {"label": label, "amount": ZERO})
        bucket["amount"] += reduction
    return sorted(buckets.values(), key=lambda b: b["amount"], reverse=True)


def interest_by_year(loan: Loan) -> dict:
    """Interest paid per calendar year + a lifetime total (payments' interest component plus any
    capitalized INTEREST transactions). The tax-time rollup — a pure register read."""
    by_year: dict[int, Decimal] = {}
    for txn in loan.transactions.all():
        if txn.txn_type in SPLIT_TYPES:
            amount = txn.interest
        elif txn.txn_type == LoanTxnType.INTEREST:
            amount = txn.amount
        else:
            amount = ZERO
        if amount:
            by_year[txn.date.year] = by_year.get(txn.date.year, ZERO) + amount
    rows = [{"year": y, "amount": a} for y, a in sorted(by_year.items(), reverse=True)]
    return {"rows": rows, "total": sum(by_year.values(), ZERO)}


def monthly_obligation(loans) -> Decimal:
    """Total scheduled payment normalized to a monthly figure across installment loans."""
    total = ZERO
    for loan in loans:
        if loan.is_installment and loan.payment_amount:
            ppy = PERIODS_PER_YEAR.get(loan.payment_frequency, 12)
            total += loan.payment_amount * Decimal(ppy) / Decimal(12)
    return total
