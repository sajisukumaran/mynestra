"""Investments → general-ledger posting: cost-in-the-ledger double entry per transaction type, the
`gl == cash + Σ open-lot cost` invariant, transfer clearing via 1150, and idempotency."""

import datetime
from decimal import Decimal

from django_tenants.utils import schema_context

from apps.finance.models import Account, Currency, JournalEntry, JournalLine
from apps.finance.services import account_balance, resolve_account
from apps.investments.models import (
    InvestmentAccount,
    InvestmentTransaction,
    InvTxnType,
    Security,
)
from apps.investments.services import (
    apply_transaction,
    cash_balance,
    cost_basis,
    ensure_gl_account,
)
from apps.organizations.models import Organization

D = Decimal
JAN = datetime.date(2026, 1, 2)


def _setup():
    org = Organization.objects.create(name="Broker")
    acct = InvestmentAccount.objects.create(
        institution=org, nickname="Taxable", registration="taxable_individual",
        currency=Currency.objects.get(code="USD"),
    )
    ensure_gl_account(acct)
    sec = Security.objects.create(
        symbol="ACME", name="Acme", currency=Currency.objects.get(code="USD"))
    return acct, sec


def _add(acct, ttype, date, **kw):
    fields = {"quantity": "0", "price": "0", "amount": "0", "fee": "0"}
    fields.update(kw)
    txn = InvestmentTransaction.objects.create(
        account=acct, txn_type=ttype, date=date,
        quantity=D(fields.pop("quantity")), price=D(fields.pop("price")),
        amount=D(fields.pop("amount")), fee=D(fields.pop("fee")), **fields,
    )
    apply_transaction(txn, is_new=True)
    txn.refresh_from_db()
    return txn


def test_opening_cash_posts_equity_and_sets_balance(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, _ = _setup()
        _add(acct, InvTxnType.OPENING, JAN, amount="1000")
        assert account_balance(acct.gl_account) == D("1000")
        assert cash_balance(acct) == D("1000")
        # Opening equity was credited.
        eq = resolve_account("opening_balance_equity")
        assert JournalLine.objects.filter(account=eq, credit=D("1000")).exists()


def test_buy_posts_no_gl_entry(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        _add(acct, InvTxnType.OPENING, JAN, amount="1000")
        before = JournalEntry.objects.count()
        buy = _add(acct, InvTxnType.BUY, datetime.date(2026, 2, 1),
                   security=sec, quantity="10", price="50", amount="500")
        assert buy.journal_entry_id is None            # cost-neutral: cash → securities
        assert JournalEntry.objects.count() == before  # no new posted entry
        assert account_balance(acct.gl_account) == D("1000")  # gl unchanged (still at cost)


def test_settlement_date_is_metadata_only(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        _add(acct, InvTxnType.OPENING, JAN, amount="1000")
        trade = datetime.date(2026, 2, 1)
        settle = datetime.date(2026, 2, 3)
        buy = _add(acct, InvTxnType.BUY, trade, security=sec,
                   quantity="10", price="50", amount="500", settlement_date=settle)
        assert buy.settlement_date == settle
        # The lot acquires on the TRADE date, not the settlement date.
        assert acct.lots.get(open=True).acquired_date == trade
        # Still cost-neutral, and the invariant holds regardless of settlement.
        assert buy.journal_entry_id is None
        assert account_balance(acct.gl_account) == cash_balance(acct) + cost_basis(acct)


def test_sell_gain_credits_realized_capital_gain(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        _add(acct, InvTxnType.OPENING, JAN, amount="1000")
        _add(acct, InvTxnType.BUY, datetime.date(2026, 2, 1),
             security=sec, quantity="10", price="50", amount="500")
        _add(acct, InvTxnType.SELL, datetime.date(2026, 3, 1),
             security=sec, quantity="10", price="70", amount="700")
        gain = resolve_account("realized_capital_gain")
        assert JournalLine.objects.filter(account=gain, credit=D("200")).exists()
        # Natural revenue balance reflects the gain.
        assert account_balance(gain) == D("200")


def test_sell_loss_debits_realized_capital_gain(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        _add(acct, InvTxnType.OPENING, JAN, amount="1000")
        _add(acct, InvTxnType.BUY, datetime.date(2026, 2, 1),
             security=sec, quantity="10", price="50", amount="500")
        _add(acct, InvTxnType.SELL, datetime.date(2026, 3, 1),
             security=sec, quantity="10", price="40", amount="400")
        gain = resolve_account("realized_capital_gain")
        assert JournalLine.objects.filter(account=gain, debit=D("100")).exists()
        assert account_balance(gain) == D("-100")


def test_dividend_interest_capgain_fee_hit_their_accounts(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        _add(acct, InvTxnType.OPENING, JAN, amount="1000")
        _add(acct, InvTxnType.DIVIDEND, datetime.date(2026, 2, 1), security=sec, amount="40")
        _add(acct, InvTxnType.INTEREST, datetime.date(2026, 2, 2), amount="10")
        _add(acct, InvTxnType.CAP_GAIN_DIST, datetime.date(2026, 2, 3), security=sec, amount="25")
        _add(acct, InvTxnType.FEE, datetime.date(2026, 2, 4), amount="5")
        assert account_balance(resolve_account("dividend_income")) == D("40")
        assert account_balance(resolve_account("investment_interest")) == D("10")
        assert account_balance(resolve_account("capital_gains_distribution")) == D("25")
        assert account_balance(resolve_account("investment_fees")) == D("5")
        # Cash: 1000 + 40 + 10 + 25 − 5 = 1070.
        assert cash_balance(acct) == D("1070")


def test_contribution_as_income_vs_equity(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, _ = _setup()
        # Plain contribution → opening equity (own funds).
        _add(acct, InvTxnType.CONTRIBUTION, JAN, amount="500")
        eq = resolve_account("opening_balance_equity")
        assert JournalLine.objects.filter(account=eq, credit=D("500")).exists()
        # Categorized as income (e.g. employer match) → that revenue account.
        income = resolve_account("4900")
        _add(acct, InvTxnType.CONTRIBUTION, datetime.date(2026, 2, 1), amount="300",
             category_account=income)
        assert account_balance(income) == D("300")


def test_gl_equals_cash_plus_cost_invariant(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct, sec = _setup()
        _add(acct, InvTxnType.OPENING, JAN, amount="1000")
        _add(acct, InvTxnType.BUY, datetime.date(2026, 2, 1),
             security=sec, quantity="10", price="50", amount="500")
        _add(acct, InvTxnType.DIVIDEND, datetime.date(2026, 3, 1), security=sec, amount="20")
        _add(acct, InvTxnType.DIVIDEND_REINVEST, datetime.date(2026, 3, 15),
             security=sec, quantity="0.5", price="60", amount="30")
        _add(acct, InvTxnType.SELL, datetime.date(2026, 4, 1),
             security=sec, quantity="5", price="60", amount="300")
        _add(acct, InvTxnType.FEE, datetime.date(2026, 5, 1), amount="10")
        assert account_balance(acct.gl_account) == cash_balance(acct) + cost_basis(acct)


def test_transfer_clearing_nets_to_zero_with_matching_bank_leg(make_tenant):
    from apps.banking.models import BankAccount
    from apps.investments.services import create_matching_leg

    with schema_context(make_tenant().schema_name):
        acct, _ = _setup()
        bank = BankAccount.objects.create(
            bank=Organization.objects.create(name="Bank"), account_type="checking",
            nickname="Checking", currency_id="USD",
        )
        # Fund the investment account from the bank (transfer in).
        txn = _add(acct, InvTxnType.TRANSFER_IN, JAN, amount="2000", counter_account=bank)
        create_matching_leg(txn)
        clearing = resolve_account("transfer_clearing")
        assert account_balance(clearing) == D("0")  # both legs net through 1150
        assert cash_balance(acct) == D("2000")
        bank.refresh_from_db()  # the matching leg provisioned the bank's GL node elsewhere
        assert bank.balance == D("-2000")           # money left the bank


def test_transfer_clearing_nets_to_zero_between_two_investment_accounts(make_tenant):
    from apps.investments.services import create_matching_leg

    with schema_context(make_tenant().schema_name):
        org = Organization.objects.create(name="Broker")
        cur = Currency.objects.get(code="USD")
        src = InvestmentAccount.objects.create(
            institution=org, nickname="Taxable", registration="taxable_individual", currency=cur)
        dst = InvestmentAccount.objects.create(
            institution=org, nickname="Roth", registration="roth_ira", currency=cur)
        ensure_gl_account(src)
        ensure_gl_account(dst)
        _add(src, InvTxnType.OPENING, JAN, amount="5000")

        # Move cash out of the taxable account into the Roth; matching leg mirrors it in the Roth.
        out = _add(src, InvTxnType.TRANSFER_OUT, datetime.date(2026, 2, 1), amount="1500",
                   counter_investment_account=dst)
        create_matching_leg(out)

        clearing = resolve_account("transfer_clearing")
        assert account_balance(clearing) == D("0")   # both legs net through 1150
        assert cash_balance(src) == D("3500")         # 5000 − 1500 left
        assert cash_balance(dst) == D("1500")         # arrived in the Roth
        mirror = dst.transactions.get(txn_type=InvTxnType.TRANSFER_IN)
        assert mirror.counter_investment_account_id == src.pk
        assert not mirror.is_managed_in_leg           # editable/deletable, unlike an in-kind mirror


def test_posting_is_idempotent(make_tenant):
    from apps.investments.services import post_transaction

    with schema_context(make_tenant().schema_name):
        acct, _ = _setup()
        txn = _add(acct, InvTxnType.OPENING, JAN, amount="1000")
        n = JournalEntry.objects.count()
        post_transaction(txn)  # re-post same version → same entry, no dup
        assert JournalEntry.objects.count() == n


def test_gl_account_nested_under_group_header(make_tenant):
    with schema_context(make_tenant().schema_name):
        org = Organization.objects.create(name="Broker")
        cur = Currency.objects.get(code="USD")
        taxable = InvestmentAccount.objects.create(
            institution=org, nickname="Tax", registration="taxable_individual", currency=cur)
        roth = InvestmentAccount.objects.create(
            institution=org, nickname="Roth", registration="roth_ira", currency=cur)
        hsa = InvestmentAccount.objects.create(
            institution=org, nickname="HSA", registration="hsa", currency=cur)
        assert ensure_gl_account(taxable).parent.code == "1210"
        assert ensure_gl_account(roth).parent.code == "1220"
        assert ensure_gl_account(hsa).parent.code == "1230"
        assert Account.objects.get(code="1210").is_postable is False  # header rolls up
