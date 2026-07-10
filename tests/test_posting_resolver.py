"""The Standard/Expert account-resolution seam: `resolve_posting_account` falls back to the
subledger default in Standard, honors per-owner PostingMap overrides in Expert, and raises when a
mapped account has been removed."""

import pytest
from django.db import connection
from django_tenants.utils import schema_context

from apps.contacts.models import Person
from apps.finance.exceptions import UnknownAccount
from apps.finance.models import Account, AccountType, PostingMap, Side
from apps.finance.services import (
    posting_map_for,
    resolve_posting_account,
    set_posting_map,
)
from apps.tenants.models import Tenant


def _set_mode(tenant, mode):
    connection.set_schema_to_public()
    Tenant.objects.filter(pk=tenant.pk).update(accounting_mode=mode)


def _owner(tenant):
    """A generic subledger-owner stand-in (any tenant model with a pk works for the seam)."""
    return Person.objects.create(first_name="Owner", last_name="Row")


def _custom_account(code="5901"):
    return Account.objects.create(
        code=code, name=f"Custom {code}", type=AccountType.EXPENSE,
        normal_side=Side.DEBIT, parent=Account.objects.get(code="5000"),
        is_postable=True, is_system=False,
    )


def test_standard_mode_uses_default(make_tenant):
    tenant = make_tenant()
    _set_mode(tenant, "standard")
    with schema_context(tenant.schema_name):
        owner = _owner(tenant)
        custom = _custom_account()
        set_posting_map(owner, "fee_expense", custom)  # present but must be ignored in Standard
        resolved = resolve_posting_account(owner, "fee_expense", "5900")
        assert resolved.code == "5900"


def test_expert_mode_honors_override(make_tenant):
    tenant = make_tenant()
    _set_mode(tenant, "expert")
    with schema_context(tenant.schema_name):
        owner = _owner(tenant)
        custom = _custom_account()
        set_posting_map(owner, "fee_expense", custom)
        resolved = resolve_posting_account(owner, "fee_expense", "5900")
        assert resolved.pk == custom.pk


def test_expert_mode_without_override_falls_back(make_tenant):
    tenant = make_tenant()
    _set_mode(tenant, "expert")
    with schema_context(tenant.schema_name):
        owner = _owner(tenant)
        resolved = resolve_posting_account(owner, "fee_expense", "5900")
        assert resolved.code == "5900"


def test_expert_mode_missing_mapped_account_raises(make_tenant):
    tenant = make_tenant()
    _set_mode(tenant, "expert")
    with schema_context(tenant.schema_name):
        owner = _owner(tenant)
        custom = _custom_account()
        set_posting_map(owner, "fee_expense", custom)
        custom.delete()  # soft-delete; the mapping is now dangling
        with pytest.raises(UnknownAccount):
            resolve_posting_account(owner, "fee_expense", "5900")


def test_set_and_read_posting_map_roundtrip(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        owner = _owner(tenant)
        custom = _custom_account()
        set_posting_map(owner, "charge_category", custom)
        assert posting_map_for(owner) == {"charge_category": custom.pk}
        set_posting_map(owner, "charge_category", None)  # clear
        assert posting_map_for(owner) == {}
        assert not PostingMap.objects.exists()


def test_no_owner_always_default(make_tenant):
    tenant = make_tenant()
    _set_mode(tenant, "expert")
    with schema_context(tenant.schema_name):
        assert resolve_posting_account(None, "fee_expense", "5900").code == "5900"
