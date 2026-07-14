"""Banking → general-ledger posting: correct double-entry per transaction type, transfers via the
clearing account, edit (reverse+repost), delete (reverse), idempotency, and holder P2O sync."""

import datetime
from decimal import Decimal

from django_tenants.utils import schema_context

from apps.banking.models import (
    AccountType,
    BankAccount,
    BankAccountHolder,
    BankTransaction,
    TxnType,
)
from apps.banking.services import (
    create_matching_leg,
    ensure_gl_account,
    post_transaction,
    register,
    repost_transaction,
    sync_holder_p2o,
    unpost_transaction,
)
from apps.contacts.models import Person
from apps.finance.models import Account, Currency, JournalEntry
from apps.finance.services import account_balance
from apps.organizations.models import Organization

D = Decimal
JAN = datetime.date(2026, 1, 15)


def _account(nickname="HDFC Checking", account_type=AccountType.CHECKING, number="1234567890"):
    bank = Organization.objects.create(name="HDFC Bank")
    return BankAccount.objects.create(
        bank=bank,
        account_type=account_type,
        nickname=nickname,
        number=number,
        currency=Currency.objects.get(code="USD"),
    )


def _txn(account, txn_type, amount, **extra):
    txn = BankTransaction.objects.create(
        account=account, txn_type=txn_type, date=JAN, amount=D(amount), **extra
    )
    post_transaction(txn)
    return txn


def test_gl_account_created_under_type_header(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        checking = _account(account_type=AccountType.CHECKING)
        savings = _account(nickname="HDFC Savings", account_type=AccountType.SAVINGS)
        gl_c = ensure_gl_account(checking)
        gl_s = ensure_gl_account(savings)
        assert gl_c.parent.code == "1120" and gl_c.code.startswith("1120.")
        assert gl_s.parent.code == "1130" and gl_s.code.startswith("1130.")
        assert gl_c.is_postable and gl_c.currency_id == "USD" and not gl_c.is_system
        assert "••7890" in gl_c.name  # masked number in the ledger account name


def test_opening_balance_posts_and_sets_balance(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        acct = _account()
        txn = _txn(acct, TxnType.OPENING, "1000")
        acct.refresh_from_db()
        assert acct.balance == D("1000")
        entry = txn.journal_entry
        assert entry.entry_type == JournalEntry.EntryType.OPENING
        assert entry.lines.count() == 2
        assert entry.lines.get(account=acct.gl_account).debit == D("1000")
        assert entry.lines.get(account__code="3100").credit == D("1000")  # opening balance equity


def test_deposit_and_withdrawal_use_category_and_payee(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        acct = _account()
        person = Person.objects.create(first_name="Acme", last_name="Employer")
        org = Organization.objects.create(name="Green Grocer")
        salary = Account.objects.get(code="4100")
        groceries = Account.objects.get(code="5200")

        dep = _txn(acct, TxnType.DEPOSIT, "500", category_account=salary, payee_person=person)
        wd = _txn(
            acct, TxnType.WITHDRAWAL, "80", category_account=groceries, payee_organization=org
        )

        acct.refresh_from_db()
        assert acct.balance == D("420")  # 500 − 80
        assert dep.journal_entry.lines.get(account=salary).person_id == person.pk
        assert dep.journal_entry.lines.get(account=acct.gl_account).debit == D("500")
        assert wd.journal_entry.lines.get(account=groceries).organization_id == org.pk
        assert wd.journal_entry.lines.get(account=acct.gl_account).credit == D("80")


def test_interest_fee_and_charge_hit_fixed_accounts(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        acct = _account()
        _txn(acct, TxnType.OPENING, "100")
        interest = _txn(acct, TxnType.INTEREST, "12")
        fee = _txn(acct, TxnType.FEE, "25")
        charge = _txn(acct, TxnType.CHARGE, "3")

        acct.refresh_from_db()
        assert acct.balance == D("84")  # 100 + 12 − 25 − 3
        assert interest.journal_entry.lines.get(account__code="4400").credit == D("12")
        assert fee.journal_entry.lines.get(account__code="5850").debit == D("25")
        assert charge.journal_entry.lines.get(account__code="5850").debit == D("3")
        # Interest/fees default their counterparty to the bank organization.
        interest_line = interest.journal_entry.lines.get(account__code="4400")
        assert interest_line.organization_id == acct.bank_id


def test_cd_opens_under_1140_and_interest_posts(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        cd = _account(nickname="12-month CD", account_type=AccountType.CD, number="5555")
        gl = ensure_gl_account(cd)
        assert gl.parent.code == "1140" and gl.code.startswith("1140.")

        _txn(cd, TxnType.OPENING, "5000")
        interest = _txn(cd, TxnType.INTEREST, "50")
        cd.refresh_from_db()
        assert cd.balance == D("5050")  # 5000 + 50 interest
        # Bank CD interest posts to 4400 (same as any bank interest).
        assert interest.journal_entry.lines.get(account__code="4400").credit == D("50")
        # The CD balance rolls up under the 1140 header and the 1100 Cash & Bank parent.
        assert account_balance(Account.objects.get(code="1140")) == D("5050")
        assert account_balance(Account.objects.get(code="1100")) == D("5050")


def test_transfer_two_legs_via_clearing_with_auto_match(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        a = _account(nickname="Checking")
        b = _account(nickname="Savings", account_type=AccountType.SAVINGS, number="9999")
        _txn(a, TxnType.OPENING, "1000")

        out = _txn(a, TxnType.TRANSFER_OUT, "300", counter_account=b)
        leg = create_matching_leg(out)

        a.refresh_from_db()
        b.refresh_from_db()
        assert a.balance == D("700")  # 1000 − 300
        assert b.balance == D("300")
        assert leg.txn_type == TxnType.TRANSFER_IN and leg.account_id == b.pk
        assert account_balance("transfer_clearing") == D("0")  # clearing nets to zero


def test_edit_reverses_and_reposts(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        acct = _account()
        txn = _txn(acct, TxnType.OPENING, "1000")
        txn.amount = D("1500")
        txn.save(update_fields=["amount"])
        repost_transaction(txn)

        acct.refresh_from_db()
        assert acct.balance == D("1500")
        assert txn.posting_version == 2
        # original entry + its reversal + the fresh entry
        assert JournalEntry.objects.count() == 3


def test_delete_reverses_to_zero(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        acct = _account()
        _txn(acct, TxnType.OPENING, "1000")
        dep = _txn(acct, TxnType.DEPOSIT, "200")
        acct.refresh_from_db()
        assert acct.balance == D("1200")
        unpost_transaction(dep)
        acct.refresh_from_db()
        assert acct.balance == D("1000")  # deposit reversed out


def test_idempotent_repost_of_same_version(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        acct = _account()
        txn = _txn(acct, TxnType.DEPOSIT, "75")
        first = txn.journal_entry
        again = post_transaction(txn)  # same posting_version → idempotent
        assert first.pk == again.pk
        assert JournalEntry.objects.count() == 1


def test_holder_p2o_sync_adds_account_holder_links(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        from apps.relationships.models import PersonOrgRelationship

        acct = _account()
        p1 = Person.objects.create(first_name="Asha", last_name="R")
        p2 = Person.objects.create(first_name="Ravi", last_name="R")
        BankAccountHolder.objects.create(account=acct, person=p1, is_primary=True)
        BankAccountHolder.objects.create(account=acct, person=p2)
        sync_holder_p2o(acct)

        links = PersonOrgRelationship.objects.filter(
            organization=acct.bank, type__code="account_holder"
        )
        assert links.count() == 2
        assert set(links.values_list("person_id", flat=True)) == {p1.pk, p2.pk}


def test_register_running_balance(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        acct = _account()
        _txn(acct, TxnType.OPENING, "1000")
        _txn(acct, TxnType.DEPOSIT, "200")
        _txn(acct, TxnType.WITHDRAWAL, "50")
        rows = register(acct)["rows"]  # newest-first
        assert [r["balance"] for r in rows] == [D("1150"), D("1200"), D("1000")]


def test_register_paginates_with_chronological_balance(make_tenant):
    """The register returns one page (50/page, newest first) with each row's own chronological
    running balance — page 2 continues where page 1 left off, and only page rows materialize."""
    import datetime

    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        acct = _account()
        base = datetime.date(2020, 1, 1)
        BankTransaction.objects.bulk_create([
            BankTransaction(
                account=acct, txn_type=TxnType.DEPOSIT,
                date=base + datetime.timedelta(days=i), amount=D("10"))
            for i in range(60)
        ])
        reg = register(acct)
        assert reg["total"] == 60
        assert len(reg["rows"]) == 50
        assert reg["rows"][0]["balance"] == D("600")   # newest row = full running total
        reg2 = register(acct, page=2)
        assert len(reg2["rows"]) == 10
        assert reg2["rows"][-1]["balance"] == D("10")  # oldest row = first deposit


def test_signed_amount_sql_matches_property_for_every_type(make_tenant):
    """`signed_amount_sql` is the SQL twin of `BankTransaction.signed_amount` — the pair must
    agree for EVERY transaction type. Lockstep guard: edit one, you must edit the other."""
    import datetime

    from apps.banking.models import signed_amount_sql

    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        acct = _account()
        BankTransaction.objects.bulk_create([
            BankTransaction(account=acct, txn_type=value, date=datetime.date(2026, 1, 2),
                            amount=D("100"))
            for value, _label in TxnType.choices
        ])
        annotated = {t.pk: t.sa for t in acct.transactions.annotate(sa=signed_amount_sql())}
        for t in acct.transactions.all():
            assert annotated[t.pk] == t.signed_amount, t.txn_type


def test_attach_balances_matches_per_account_and_stays_flat(make_tenant):
    """`attach_balances` stamps the SAME figures the per-account properties compute, in a fixed
    number of grouped queries however many accounts there are — the batch path dashboards, list
    pages and the launcher use instead of one subtree walk + aggregate per account."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from apps.banking.services import attach_balances

    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        a1 = _account(nickname="A1", number="111")
        a2 = _account(nickname="A2", number="222")
        _txn(a1, TxnType.OPENING, "1000")
        _txn(a1, TxnType.WITHDRAWAL, "250")
        _txn(a2, TxnType.OPENING, "500")

        # Per-account slow path (fresh instances → no stamps).
        expected = {a.pk: (a.balance, a.display_balance) for a in BankAccount.objects.all()}
        fresh = list(BankAccount.objects.all())
        with CaptureQueriesContext(connection) as ctx:
            attach_balances(fresh)
            got = {a.pk: (a.balance, a.display_balance) for a in fresh}
        assert got == expected
        assert expected[a1.pk][0] == D("750")
        # gl-account load + COA tree scan + grouped base + grouped native = 4, however many rows.
        assert len(ctx.captured_queries) <= 4
