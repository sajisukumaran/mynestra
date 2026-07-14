"""Loans GL/service layer — the A1–A10 posting matrix, the co-signer/external path, the
tracked-bank clearing, edit/delete lifecycle, the invariant, and the pure-read overlays."""

import datetime
from decimal import Decimal

from django_tenants.utils import schema_context

from apps.finance.models import AccountType, JournalEntry, Side
from apps.finance.services import account_balance, net_worth
from apps.loans.amortization import payoff_projection
from apps.loans.models import (
    BorrowerRole,
    Funding,
    Loan,
    LoanBorrower,
    LoanTransaction,
    LoanTxnType,
    LoanType,
)
from apps.loans.services import (
    contributions_by_borrower,
    create_matching_leg,
    delete_transaction,
    interest_by_year,
    register,
    repost_transaction,
)
from apps.loans.services import post_transaction as post

D = Decimal
ZERO = D("0")
JAN = datetime.date(2026, 1, 15)


def _org(name="Lender"):
    from apps.organizations.models import Organization

    return Organization.objects.create(name=name)


def _person(first="Aarav", last="Kumar"):
    from apps.contacts.models import Person

    return Person.objects.create(first_name=first, last_name=last)


def _usd():
    from apps.finance.models import Currency

    return Currency.objects.get(code="USD")


def _loan(**kw):
    defaults = {"loan_type": LoanType.AUTO, "nickname": "Auto loan", "currency": _usd()}
    defaults.update(kw)
    if "lender_person" not in defaults and "lender_organization" not in defaults:
        defaults["lender_organization"] = _org()
    return Loan.objects.create(**defaults)


def _bank_account(nickname="Checking"):
    from apps.banking.models import AccountType as BAT
    from apps.banking.models import BankAccount
    from apps.banking.services import ensure_gl_account as bank_gl

    acct = BankAccount.objects.create(
        bank=_org("My Bank"), account_type=BAT.CHECKING, nickname=nickname, currency=_usd()
    )
    bank_gl(acct)
    return acct


def _txn(loan, txn_type, amount, *, post_it=True, **kw):
    txn = LoanTransaction.objects.create(
        loan=loan, txn_type=txn_type, date=kw.pop("date", JAN), amount=amount, **kw
    )
    if post_it:
        post(txn)
    return txn


def _invariant_holds(loan) -> bool:
    delta = sum((t.balance_delta for t in loan.transactions.all()), ZERO)
    return account_balance(loan.gl_account) == delta


# --- A1 / provisioning --------------------------------------------------------------------

def test_opening_creates_node_under_type_header(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        loan = _loan(loan_type=LoanType.AUTO, principal_original=D("20000"))
        _txn(loan, LoanTxnType.OPENING, D("20000"))
        gl = loan.gl_account
        assert gl.parent.code == "2220"  # Auto Loan header
        assert gl.code == "2220.01"
        assert gl.type == AccountType.LIABILITY and gl.normal_side == Side.CREDIT
        assert loan.balance == D("20000")
        assert _invariant_holds(loan)


def test_contingent_loan_excluded_from_net_worth(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        cosigned = _loan(
            loan_type=LoanType.AUTO, nickname="Son's car",
            counts_toward_net_worth=False, principal_original=D("15000"),
        )
        _txn(cosigned, LoanTxnType.OPENING, D("15000"))
        assert cosigned.gl_account.parent.code == "2950"  # Contingent Liabilities
        assert net_worth() == ZERO  # off net worth
        assert net_worth(include_contingent=True) == D("-15000")

        mortgage = _loan(
            loan_type=LoanType.MORTGAGE, nickname="Home", principal_original=D("100000")
        )
        _txn(mortgage, LoanTxnType.OPENING, D("100000"))
        assert mortgage.gl_account.parent.code == "2210"
        assert net_worth() == D("-100000")  # only the mortgage counts


# --- A3 / A4 / A5: payment funding modes --------------------------------------------------

def test_payment_from_bank_splits_and_clears(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        loan = _loan(loan_type=LoanType.AUTO, principal_original=D("20000"))
        _txn(loan, LoanTxnType.OPENING, D("20000"))
        bank = _bank_account()
        pay = _txn(
            loan, LoanTxnType.PAYMENT, D("540"),
            principal=D("400"), interest=D("100"), escrow=D("40"),
            funding_source=Funding.BANK, funding_account=bank,
        )
        create_matching_leg(pay)
        pay.refresh_from_db()
        assert loan.balance == D("19600")            # only principal (400) reduces the liability
        assert account_balance("5860") == D("100")   # interest expense
        assert account_balance("5140") == D("40")    # escrow → property tax
        assert account_balance("1150") == ZERO       # clearing nets across modules
        assert pay.bank_txn_id is not None
        assert _invariant_holds(loan)


def test_payment_from_cash(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        loan = _loan(principal_original=D("5000"))
        _txn(loan, LoanTxnType.OPENING, D("5000"))
        _txn(
            loan, LoanTxnType.PAYMENT, D("300"),
            principal=D("250"), interest=D("50"), funding_source=Funding.CASH,
        )
        assert loan.balance == D("4750")
        assert account_balance("5860") == D("50")
        assert account_balance("1110") == D("-300")  # cash on hand drops by the full payment
        assert _invariant_holds(loan)


def test_external_payment_by_another_party(make_tenant):
    """The co-signer scenario: the son pays from an untracked account. Only the principal reduces
    the balance (against opening equity, tagged with the payer); interest is NOT booked."""
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        son = _person("Rohan", "Shah")
        loan = _loan(loan_type=LoanType.AUTO, principal_original=D("20000"))
        _txn(loan, LoanTxnType.OPENING, D("20000"))
        interest_before = account_balance("5860")
        nw_before = net_worth()
        _txn(
            loan, LoanTxnType.PAYMENT, D("560"),
            principal=D("450"), interest=D("110"),
            funding_source=Funding.EXTERNAL, payer_person=son,
        )
        assert loan.balance == D("19550")             # reduced by principal only (450)
        assert account_balance("5860") == interest_before  # interest NOT booked as our expense
        # Net worth rises by the principal paid down (correct — a co-signed debt shrank, our books
        # spent no tracked cash); the offset lands in opening equity, tagged with the payer.
        assert net_worth() == nw_before + D("450")
        assert account_balance("opening_balance_equity") == D("-19550")
        assert _invariant_holds(loan)


# --- A2 disbursement / A7 draw / A8 interest / A9 fee -------------------------------------

def test_disbursement_into_bank_clears(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        loan = _loan(loan_type=LoanType.PERSONAL, principal_original=D("10000"))
        bank = _bank_account()
        disb = _txn(
            loan, LoanTxnType.DISBURSEMENT, D("10000"),
            funding_source=Funding.BANK, funding_account=bank,
        )
        create_matching_leg(disb)
        assert loan.balance == D("10000")        # you now owe the loan
        assert bank.balance == D("10000")        # cash landed in checking
        assert account_balance("1150") == ZERO   # clearing nets
        assert _invariant_holds(loan)


def test_revolving_draw_interest_fee(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        loan = _loan(loan_type=LoanType.HELOC, nickname="HELOC", credit_limit=D("50000"))
        _txn(loan, LoanTxnType.DRAW, D("10000"), funding_source=Funding.CASH)
        _txn(loan, LoanTxnType.INTEREST, D("75"))
        _txn(loan, LoanTxnType.FEE, D("25"))
        assert loan.gl_account.parent.code == "2250"      # HELOC header
        assert loan.balance == D("10100")                 # draw + interest + fee
        assert account_balance("5860") == D("75")
        assert account_balance("5850") == D("25")
        assert loan.available_credit == D("39900")
        assert _invariant_holds(loan)


# --- lifecycle: edit / delete ------------------------------------------------------------

def test_repost_rebuilds_matched_leg(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        loan = _loan(principal_original=D("20000"))
        _txn(loan, LoanTxnType.OPENING, D("20000"))
        bank = _bank_account()
        pay = _txn(
            loan, LoanTxnType.PAYMENT, D("500"), principal=D("400"), interest=D("100"),
            funding_source=Funding.BANK, funding_account=bank,
        )
        create_matching_leg(pay)
        old_leg_id = pay.bank_txn_id
        # Edit: bump the principal.
        pay.amount = D("600")
        pay.principal = D("500")
        pay.interest = D("100")
        pay.save()
        repost_transaction(pay)
        pay.refresh_from_db()
        assert pay.posting_version == 2
        assert pay.bank_txn_id is not None and pay.bank_txn_id != old_leg_id
        assert loan.balance == D("19500")       # 20000 − 500
        assert account_balance("1150") == ZERO  # clearing still nets after the edit
        assert _invariant_holds(loan)


def test_delete_hard_erases_entry_and_leg(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        from apps.banking.models import BankTransaction

        loan = _loan(principal_original=D("20000"))
        _txn(loan, LoanTxnType.OPENING, D("20000"))
        bank = _bank_account()
        pay = _txn(
            loan, LoanTxnType.PAYMENT, D("500"), principal=D("400"), interest=D("100"),
            funding_source=Funding.BANK, funding_account=bank,
        )
        create_matching_leg(pay)
        entry_id = pay.journal_entry_id
        leg_id = pay.bank_txn_id
        delete_transaction(pay)
        assert not LoanTransaction.all_objects.filter(pk=pay.pk).exists()
        assert not JournalEntry.all_objects.filter(pk=entry_id).exists()  # truly erased
        assert not BankTransaction.all_objects.filter(pk=leg_id).exists()
        assert loan.balance == D("20000")  # back to the opening balance
        assert _invariant_holds(loan)


# --- read models / overlays ---------------------------------------------------------------

def test_contributions_by_borrower(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        son = _person("Rohan", "Shah")
        loan = _loan(loan_type=LoanType.AUTO, principal_original=D("20000"))
        LoanBorrower.objects.create(loan=loan, person=son, role=BorrowerRole.PRIMARY)
        _txn(loan, LoanTxnType.OPENING, D("20000"))
        # I paid twice from cash; the son paid once externally.
        _txn(loan, LoanTxnType.PAYMENT, D("500"), principal=D("450"), interest=D("50"),
             funding_source=Funding.CASH)
        _txn(loan, LoanTxnType.PAYMENT, D("500"), principal=D("460"), interest=D("40"),
             funding_source=Funding.CASH)
        _txn(loan, LoanTxnType.PAYMENT, D("500"), principal=D("470"), interest=D("30"),
             funding_source=Funding.EXTERNAL, payer_person=son)
        rows = contributions_by_borrower(loan)
        by_label = {r["label"]: r["amount"] for r in rows}
        assert by_label["You / household"] == D("910")   # 450 + 460
        assert by_label["Rohan Shah"] == D("470")
        assert _invariant_holds(loan)


def test_interest_by_year(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        loan = _loan(principal_original=D("20000"))
        _txn(loan, LoanTxnType.OPENING, D("20000"))
        _txn(loan, LoanTxnType.PAYMENT, D("500"), principal=D("400"), interest=D("100"),
             funding_source=Funding.CASH, date=datetime.date(2025, 6, 1))
        _txn(loan, LoanTxnType.PAYMENT, D("500"), principal=D("420"), interest=D("80"),
             funding_source=Funding.CASH, date=datetime.date(2026, 6, 1))
        _txn(loan, LoanTxnType.INTEREST, D("25"), date=datetime.date(2026, 7, 1))
        summary = interest_by_year(loan)
        by_year = {r["year"]: r["amount"] for r in summary["rows"]}
        assert by_year[2025] == D("100")
        assert by_year[2026] == D("105")   # 80 + 25
        assert summary["total"] == D("205")


def test_payoff_projection_posts_nothing(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        loan = _loan(
            loan_type=LoanType.MORTGAGE, nickname="Home", principal_original=D("100000"),
            annual_rate=D("6"), term_months=360, payment_amount=D("599.55"),
            payment_frequency="monthly", start_date=datetime.date(2026, 1, 1),
        )
        _txn(loan, LoanTxnType.OPENING, D("100000"))
        before = JournalEntry.objects.count()
        proj = payoff_projection(loan)
        assert JournalEntry.objects.count() == before      # pure read — posts nothing
        assert proj["payoff_date"] is not None
        assert proj["remaining_interest"] > ZERO
        assert proj["balance_series"][0][1] == D("100000")


def test_register_running_balance(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        loan = _loan(principal_original=D("10000"))
        _txn(loan, LoanTxnType.OPENING, D("10000"))
        _txn(loan, LoanTxnType.PAYMENT, D("500"), principal=D("400"), interest=D("100"),
             funding_source=Funding.CASH, date=datetime.date(2026, 2, 15))
        rows = register(loan)          # newest first
        assert rows[0]["balance"] == D("9600")   # after the payment
        assert rows[-1]["balance"] == D("10000") # after the opening
        assert _invariant_holds(loan)
