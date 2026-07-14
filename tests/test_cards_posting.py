"""Credit-card → general-ledger posting: correct double-entry per type, payment auto-match through
the 1150 clearing account, Expert-mode category remap, edit (reverse+repost), delete (reverse),
balance owed / available credit / utilization, and cardholder P2O sync."""

import datetime
from decimal import Decimal

from django.db import connection
from django_tenants.utils import schema_context

from apps.banking.models import BankAccount
from apps.banking.services import ensure_gl_account as ensure_bank_gl
from apps.cards.models import CardTxnType, CreditCard, CreditCardHolder, CreditCardTransaction
from apps.cards.services import (
    create_matching_leg,
    ensure_gl_account,
    post_transaction,
    register,
    repost_transaction,
    sync_holder_p2o,
    unpost_transaction,
)
from apps.contacts.models import Person
from apps.finance.models import Account, AccountType, Currency, JournalEntry, Side
from apps.finance.services import account_balance, set_posting_map
from apps.organizations.models import Organization
from apps.tenants.models import Tenant

D = Decimal
JAN = datetime.date(2026, 1, 15)


def _expert(tenant):
    connection.set_schema_to_public()
    Tenant.objects.filter(pk=tenant.pk).update(accounting_mode="expert")


def _card(nickname="Amex Gold", number="123456789012", limit="5000"):
    issuer = Organization.objects.create(name="American Express")
    return CreditCard.objects.create(
        issuer=issuer, nickname=nickname, number=number,
        currency=Currency.objects.get(code="USD"), credit_limit=D(limit),
    )


def _txn(card, txn_type, amount, **extra):
    txn = CreditCardTransaction.objects.create(
        card=card, txn_type=txn_type, date=JAN, amount=D(amount), **extra
    )
    post_transaction(txn)
    return txn


def _bank_account():
    bank = Organization.objects.create(name="HDFC Bank")
    acct = BankAccount.objects.create(
        bank=bank, account_type="checking", nickname="Chk", currency_id="USD"
    )
    ensure_bank_gl(acct)
    return acct


def test_gl_account_created_under_credit_card_header(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        card = _card()
        gl = ensure_gl_account(card)
        assert gl.parent.system_key == "credit_cards" and gl.code.startswith("2100.")
        assert gl.type == AccountType.LIABILITY and gl.normal_side == Side.CREDIT
        assert gl.is_postable and not gl.is_system
        assert "••9012" in gl.name


def test_opening_balance_owed(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        card = _card()
        txn = _txn(card, CardTxnType.OPENING, "1000")
        card.refresh_from_db()
        assert card.balance == D("1000")  # positive = owed
        entry = txn.journal_entry
        assert entry.entry_type == JournalEntry.EntryType.OPENING
        # Dr Opening Balance Equity / Cr the card liability
        assert entry.lines.get(debit__gt=0).account.system_key == "opening_balance_equity"
        assert entry.lines.get(credit__gt=0).account.pk == card.gl_account_id


def test_charge_increases_balance_with_payee(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        card = _card()
        merchant = Organization.objects.create(name="Whole Foods")
        _txn(card, CardTxnType.CHARGE, "200", payee_organization=merchant)
        card.refresh_from_db()
        assert card.balance == D("200")
        txn2 = CreditCardTransaction.objects.filter(card=card).first()
        expense_line = txn2.journal_entry.lines.get(debit__gt=0)
        assert expense_line.account.type == AccountType.EXPENSE
        assert expense_line.organization_id == merchant.pk


def test_charge_with_explicit_category(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        card = _card()
        food = Account.objects.get(code="5200")  # Food & Groceries
        txn = _txn(card, CardTxnType.CHARGE, "45", category_account=food)
        assert txn.journal_entry.lines.get(debit__gt=0).account.pk == food.pk


def test_interest_and_fee_hit_expense_accounts(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        card = _card()
        interest = _txn(card, CardTxnType.INTEREST, "12")
        fee = _txn(card, CardTxnType.FEE, "99")
        int_line = interest.journal_entry.lines.get(debit__gt=0)
        assert int_line.account.system_key == "interest_expense"
        assert fee.journal_entry.lines.get(debit__gt=0).account.system_key == "bank_charges"
        assert int_line.organization_id == card.issuer_id  # tagged with the issuer org


def test_refund_and_statement_credit_reduce_balance(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        card = _card()
        _txn(card, CardTxnType.CHARGE, "500")
        _txn(card, CardTxnType.REFUND, "50")
        credit = _txn(card, CardTxnType.CREDIT, "25")  # cashback / statement credit
        card.refresh_from_db()
        assert card.balance == D("425")  # 500 - 50 - 25
        # statement credit is income
        assert credit.journal_entry.lines.get(credit__gt=0).account.code == "4900"


def test_payment_auto_match_nets_clearing_to_zero(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        card = _card()
        _txn(card, CardTxnType.OPENING, "1000")
        bank = _bank_account()
        pay = CreditCardTransaction.objects.create(
            card=card, txn_type=CardTxnType.PAYMENT, date=JAN, amount=D("300"),
            counter_account=bank,
        )
        post_transaction(pay)
        create_matching_leg(pay)
        card.refresh_from_db()
        assert card.balance == D("700")  # 1000 - 300
        clearing = Account.objects.get(system_key="transfer_clearing")
        assert account_balance(clearing) == D("0")
        # the matching bank withdrawal reduced the checking balance
        bank.refresh_from_db()
        assert bank.balance == D("-300")


def test_expert_mode_remaps_charge_category(make_tenant):
    tenant = make_tenant()
    _expert(tenant)
    with schema_context(tenant.schema_name):
        card = _card()
        ensure_gl_account(card)
        custom = Account.objects.create(
            code="5155", name="Dining", type=AccountType.EXPENSE, normal_side=Side.DEBIT,
            parent=Account.objects.get(code="5000"), is_postable=True, is_system=False,
        )
        set_posting_map(card, "charge_category", custom)
        txn = _txn(card, CardTxnType.CHARGE, "60")
        assert txn.journal_entry.lines.get(debit__gt=0).account.pk == custom.pk


def test_edit_is_reverse_and_repost(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        card = _card()
        txn = _txn(card, CardTxnType.CHARGE, "100")
        txn.amount = D("150")
        txn.save()
        repost_transaction(txn)
        txn.refresh_from_db()
        card.refresh_from_db()
        assert txn.posting_version == 2
        assert card.balance == D("150")  # original 100 reversed, 150 reposted
        assert JournalEntry.objects.filter(status=JournalEntry.Status.POSTED).count() == 3


def test_delete_reverses_to_zero(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        card = _card()
        txn = _txn(card, CardTxnType.CHARGE, "80")
        unpost_transaction(txn)
        card.refresh_from_db()
        assert card.balance == D("0")


def test_available_credit_and_utilization(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        card = _card(limit="1000")
        _txn(card, CardTxnType.CHARGE, "250")
        card.refresh_from_db()
        assert card.available_credit == D("750")
        assert round(card.utilization) == 25
        assert card.utilization_tint == ""  # under 70%


def test_register_running_balance_newest_first(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        card = _card()
        _txn(card, CardTxnType.OPENING, "100")
        _txn(card, CardTxnType.CHARGE, "50")
        rows = register(card)["rows"]
        assert [r["balance"] for r in rows] == [D("150"), D("100")]  # newest-first


def test_register_paginates_with_chronological_balance(make_tenant):
    """The card register returns one page (50/page, newest first) with each row's own chronological
    running balance owed — page 2 continues where page 1 left off."""
    import datetime

    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        card = _card()
        base = datetime.date(2020, 1, 1)
        CreditCardTransaction.objects.bulk_create([
            CreditCardTransaction(
                card=card, txn_type=CardTxnType.CHARGE,
                date=base + datetime.timedelta(days=i), amount=D("10"))
            for i in range(60)
        ])
        reg = register(card)
        assert reg["total"] == 60
        assert len(reg["rows"]) == 50
        assert reg["rows"][0]["balance"] == D("600")   # newest row = full balance owed
        reg2 = register(card, page=2)
        assert len(reg2["rows"]) == 10
        assert reg2["rows"][-1]["balance"] == D("10")  # oldest row = first charge


def test_signed_amount_sql_matches_property_for_every_type(make_tenant):
    """`signed_amount_sql` is the SQL twin of `CreditCardTransaction.signed_amount` — the pair
    must agree for EVERY transaction type. Lockstep guard: edit one, you must edit the other."""
    import datetime

    from apps.cards.models import signed_amount_sql

    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        card = _card()
        CreditCardTransaction.objects.bulk_create([
            CreditCardTransaction(card=card, txn_type=value, date=datetime.date(2026, 1, 2),
                                  amount=D("100"))
            for value, _label in CardTxnType.choices
        ])
        annotated = {t.pk: t.sa for t in card.transactions.annotate(sa=signed_amount_sql())}
        for t in card.transactions.all():
            assert annotated[t.pk] == t.signed_amount, t.txn_type


def test_cardholder_p2o_sync(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        from apps.relationships.models import PersonOrgRelationship

        card = _card()
        person = Person.objects.create(first_name="Raj", last_name="S")
        CreditCardHolder.objects.create(card=card, person=person, is_primary=True)
        sync_holder_p2o(card)
        assert PersonOrgRelationship.objects.filter(
            person=person, organization=card.issuer, type__code="cardholder"
        ).exists()


def test_attach_balances_matches_per_card_and_stays_flat(make_tenant):
    """`attach_balances` stamps the SAME figures the per-card properties compute, in a fixed
    number of grouped queries however many cards there are — the batch path the dashboard, list
    page and launcher 'Owed' tile use instead of one subtree walk + aggregate per card."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from apps.cards.services import attach_balances

    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        c1 = _card(nickname="C1", number="411111111111")
        c2 = _card(nickname="C2", number="422222222222")
        _txn(c1, CardTxnType.CHARGE, "300")
        _txn(c1, CardTxnType.PAYMENT, "100", counter_external="HDFC")
        _txn(c2, CardTxnType.CHARGE, "40")

        expected = {c.pk: (c.balance, c.display_balance) for c in CreditCard.objects.all()}
        fresh = list(CreditCard.objects.all())
        with CaptureQueriesContext(connection) as ctx:
            attach_balances(fresh)
            got = {c.pk: (c.balance, c.display_balance) for c in fresh}
        assert got == expected
        assert expected[c1.pk][0] == D("200")
        # gl-account load + COA tree scan + grouped base + grouped native = 4, however many rows.
        assert len(ctx.captured_queries) <= 4
