"""Banking under Expert mode: the per-account Accounting Setup tab remaps category legs, while
Standard-mode postings are unchanged even if a mapping exists. Also the Expert GL-node choice."""

import datetime
from decimal import Decimal

from django.db import connection
from django_tenants.utils import schema_context

from apps.banking.models import AccountType, BankAccount, BankTransaction, TxnType
from apps.banking.services import ensure_gl_account, post_transaction
from apps.finance.models import Account, Currency, Side
from apps.finance.models import AccountType as GLType
from apps.finance.services import set_posting_map
from apps.organizations.models import Organization
from apps.tenants.models import Membership, Role, Tenant

D = Decimal
JAN = datetime.date(2026, 1, 15)


def _set_mode(tenant, mode):
    connection.set_schema_to_public()
    Tenant.objects.filter(pk=tenant.pk).update(accounting_mode=mode)


def _account(nickname="HDFC Checking", account_type=AccountType.CHECKING):
    bank = Organization.objects.create(name="HDFC Bank")
    return BankAccount.objects.create(
        bank=bank, account_type=account_type, nickname=nickname, number="1234567890",
        currency=Currency.objects.get(code="USD"),
    )


def _custom_expense(code="5901", name="Streaming"):
    return Account.objects.create(
        code=code, name=name, type=GLType.EXPENSE, normal_side=Side.DEBIT,
        parent=Account.objects.get(code="5000"), is_postable=True, is_system=False,
    )


def _fee_contra(txn):
    """The debit (expense) leg of a fee entry."""
    return txn.journal_entry.lines.get(debit__gt=0).account


def test_standard_fee_uses_default_even_with_mapping(make_tenant):
    tenant = make_tenant()
    _set_mode(tenant, "standard")
    with schema_context(tenant.schema_name):
        acct = _account()
        ensure_gl_account(acct)
        set_posting_map(acct, "fee_expense", _custom_expense())  # ignored in Standard
        txn = BankTransaction.objects.create(
            account=acct, txn_type=TxnType.FEE, date=JAN, amount=D("9")
        )
        post_transaction(txn)
        assert _fee_contra(txn).system_key == "bank_charges"


def test_expert_fee_uses_mapped_account(make_tenant):
    tenant = make_tenant()
    _set_mode(tenant, "expert")
    with schema_context(tenant.schema_name):
        acct = _account()
        ensure_gl_account(acct)
        custom = _custom_expense()
        set_posting_map(acct, "fee_expense", custom)
        txn = BankTransaction.objects.create(
            account=acct, txn_type=TxnType.FEE, date=JAN, amount=D("9")
        )
        post_transaction(txn)
        assert _fee_contra(txn).pk == custom.pk


def test_expert_interest_uses_mapped_income(make_tenant):
    tenant = make_tenant()
    _set_mode(tenant, "expert")
    with schema_context(tenant.schema_name):
        acct = _account()
        ensure_gl_account(acct)
        custom = Account.objects.create(
            code="4901", name="Bonus interest", type=GLType.REVENUE, normal_side=Side.CREDIT,
            parent=Account.objects.get(code="4000"), is_postable=True, is_system=False,
        )
        set_posting_map(acct, "interest_income", custom)
        txn = BankTransaction.objects.create(
            account=acct, txn_type=TxnType.INTEREST, date=JAN, amount=D("5")
        )
        post_transaction(txn)
        assert txn.journal_entry.lines.get(credit__gt=0).account.pk == custom.pk


def test_ensure_gl_account_adopts_existing(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        acct = _account()
        existing = Account.objects.create(
            code="1191", name="My cash pot", type=GLType.ASSET, normal_side=Side.DEBIT,
            parent=Account.objects.get(code="1000"), is_postable=True, is_system=False,
        )
        gl = ensure_gl_account(acct, existing=existing)
        assert gl.pk == existing.pk
        acct.refresh_from_db()
        assert acct.gl_account_id == existing.pk


# --- Through the account-create view (Accounting tab persists a mapping) ---------------------

def _owner(make_tenant, make_user):
    tenant = make_tenant(name="Acme")
    owner = make_user("owner@example.com")
    Membership.objects.create(user=owner, tenant=tenant, role=Role.OWNER)
    return tenant, owner


def test_create_form_renders_accounting_tab_in_expert(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    _set_mode(tenant, "expert")
    client.force_login(owner)
    resp = client.get(f"/t/{tenant.schema_name}/banking/accounts/new/")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert 'name="map_fee_expense"' in body and 'name="gl_mode"' in body


def test_create_form_hides_accounting_tab_in_standard(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    _set_mode(tenant, "standard")
    client.force_login(owner)
    resp = client.get(f"/t/{tenant.schema_name}/banking/accounts/new/")
    assert resp.status_code == 200
    assert 'name="map_fee_expense"' not in resp.content.decode()


def test_create_view_persists_posting_map_in_expert(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    _set_mode(tenant, "expert")
    with schema_context(tenant.schema_name):
        bank = Organization.objects.create(name="HDFC Bank")
        custom = _custom_expense()
    client.force_login(owner)
    resp = client.post(
        f"/t/{tenant.schema_name}/banking/accounts/new/",
        {
            "bank": bank.pk, "account_type": "checking", "nickname": "Main",
            "currency": "USD", "is_active": "on", "gl_mode": "auto",
            "map_fee_expense": custom.pk,
        },
    )
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        acct = BankAccount.objects.get(nickname="Main")
        txn = BankTransaction.objects.create(
            account=acct, txn_type=TxnType.FEE, date=JAN, amount=D("4")
        )
        post_transaction(txn)
        assert txn.journal_entry.lines.get(debit__gt=0).account.pk == custom.pk
