"""Expert-mode Chart-of-Accounts editor: create/edit/reparent/delete guards, and the sticky
`accounting_locked` flag (set by Standard-critical edits to built-in accounts, not benign ones)."""

import datetime
from decimal import Decimal

import pytest
from django.db import connection
from django_tenants.utils import schema_context

from apps.finance.exceptions import COAEditError
from apps.finance.models import Account, AccountType
from apps.finance.services import (
    LineInput,
    create_account,
    delete_account,
    edit_account,
    post_entry,
)
from apps.tenants.models import Membership, Role, Tenant

D = Decimal


def _expert(tenant):
    connection.set_schema_to_public()
    Tenant.objects.filter(pk=tenant.pk).update(accounting_mode="expert")


def _locked(tenant):
    connection.set_schema_to_public()
    tenant.refresh_from_db()
    return tenant.accounting_locked


def _edit_kwargs(acct, **over):
    base = dict(
        code=acct.code, name=acct.name, account_type=acct.type,
        parent=acct.parent, is_postable=acct.is_postable, is_active=acct.is_active,
        description=acct.description,
    )
    base.update(over)
    return base


# --- Create ---------------------------------------------------------------------------------

def test_create_account(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        acct = create_account(
            # 5155: an unseeded code (5150 became the seeded Home Insurance account).
            code="5155", name="Subscriptions", account_type=AccountType.EXPENSE,
            parent=Account.objects.get(code="5000"),
        )
        assert acct.pk and acct.is_postable and not acct.is_system
        assert acct.normal_side == "debit"  # derived for an expense


def test_create_duplicate_code_rejected(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        with pytest.raises(COAEditError):
            create_account(code="5000", name="Dup", account_type=AccountType.EXPENSE)


# --- Edit / reparent guards -----------------------------------------------------------------

def test_rename_system_account_does_not_lock(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        acct = Account.objects.get(code="1110")  # Cash on Hand (system)
        edit_account(acct, **_edit_kwargs(acct, name="Petty cash"))
    assert _locked(tenant) is False


def test_recode_system_account_locks(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        acct = Account.objects.get(code="1110")
        edit_account(acct, **_edit_kwargs(acct, code="1111"))
    assert _locked(tenant) is True


def test_lines_bearing_account_cannot_become_header(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        post_entry(
            date=datetime.date(2026, 1, 5),
            lines=[
                LineInput("1110", debit=D("100")),
                LineInput("opening_balance_equity", credit=D("100")),
            ],
        )
        acct = Account.objects.get(code="1110")
        with pytest.raises(COAEditError):
            edit_account(acct, **_edit_kwargs(acct, is_postable=False))


def test_account_with_children_cannot_become_postable(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        header = Account.objects.get(code="1100")  # Cash & Bank (has children)
        with pytest.raises(COAEditError):
            edit_account(header, **_edit_kwargs(header, is_postable=True))


def test_reparent_cycle_rejected(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        root = Account.objects.get(code="1000")  # Assets
        child = Account.objects.get(code="1100")  # its child
        with pytest.raises(COAEditError):
            edit_account(root, **_edit_kwargs(root, parent=child))


# --- Delete guards + lock -------------------------------------------------------------------

def test_delete_account_with_postings_rejected(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        post_entry(
            date=datetime.date(2026, 1, 5),
            lines=[
                LineInput("1110", debit=D("50")),
                LineInput("opening_balance_equity", credit=D("50")),
            ],
        )
        with pytest.raises(COAEditError):
            delete_account(Account.objects.get(code="1110"))


def test_delete_account_with_children_rejected(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        with pytest.raises(COAEditError):
            delete_account(Account.objects.get(code="1100"))


def test_delete_clean_system_account_locks(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        acct = Account.objects.get(code="1110")  # no postings/children in a fresh tenant
        delete_account(acct)
        assert not Account.objects.filter(code="1110").exists()  # soft-deleted
    assert _locked(tenant) is True


def test_delete_custom_account_does_not_lock(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        custom = create_account(code="5199", name="Temp", account_type=AccountType.EXPENSE)
        delete_account(custom)
    assert _locked(tenant) is False


# --- View gating ----------------------------------------------------------------------------

def _owner(make_tenant, make_user):
    tenant = make_tenant(name="Acme")
    owner = make_user("owner@example.com")
    Membership.objects.create(user=owner, tenant=tenant, role=Role.OWNER)
    return tenant, owner


def test_owner_creates_account_via_view(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    _expert(tenant)
    client.force_login(owner)
    resp = client.post(
        f"/t/{tenant.schema_name}/finance/accounts/new/",
        {"code": "5155", "name": "Hobbies", "type": "EXPENSE", "parent": ""},
    )
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        assert Account.objects.filter(code="5155", name="Hobbies").exists()


def test_member_cannot_edit_accounts(make_tenant, make_user, client):
    tenant, _o = _owner(make_tenant, make_user)
    _expert(tenant)
    member = make_user("member@example.com")
    Membership.objects.create(user=member, tenant=tenant, role=Role.MEMBER)
    client.force_login(member)
    resp = client.post(
        f"/t/{tenant.schema_name}/finance/accounts/new/",
        {"code": "5156", "name": "Nope", "type": "EXPENSE"},
    )
    assert resp.status_code == 403


def test_editor_404_in_standard(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)  # default Standard
    client.force_login(owner)
    resp = client.post(
        f"/t/{tenant.schema_name}/finance/accounts/new/",
        {"code": "5157", "name": "Hidden", "type": "EXPENSE"},
    )
    assert resp.status_code == 404
