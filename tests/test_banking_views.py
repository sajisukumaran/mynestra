"""Banking screens: dashboard, accounts list, account create/detail, transaction capture, and the
member/non-member gate. Drives the authenticated tenant client end-to-end."""

from django_tenants.utils import schema_context

from apps.banking.models import BankAccount, BankTransaction
from apps.organizations.models import Organization
from apps.setup.models import Category
from apps.tenants.models import Membership, Role


def _owner(make_tenant, make_user, name="Ledgerton", email="owner@example.com"):
    tenant = make_tenant(name=name)
    owner = make_user(email)
    Membership.objects.create(user=owner, tenant=tenant, role=Role.OWNER)
    return tenant, owner


def _bank(name="HDFC Bank"):
    org = Organization.objects.create(name=name)
    org.categories.add(Category.objects.get(kind="ORG", name="Bank"))
    return org


def _url(tenant, path=""):
    return f"/t/{tenant.schema_name}/banking/{path}"


def test_dashboard_and_list_render_empty(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    assert client.get(_url(tenant)).status_code == 200
    body = client.get(_url(tenant, "accounts/")).content.decode()
    assert "No accounts yet" in body


def test_non_member_is_denied(make_tenant, make_user, client):
    tenant, _o = _owner(make_tenant, make_user)
    outsider = make_user("outsider@example.com")
    client.force_login(outsider)
    assert client.get(_url(tenant)).status_code == 403


def test_account_create_form_renders(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        _bank()
    client.force_login(owner)
    body = client.get(_url(tenant, "accounts/new/")).content.decode()
    assert "New account" in body and "HDFC Bank" in body


def test_create_account_with_opening_balance(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        bank = _bank()
    client.force_login(owner)
    resp = client.post(
        _url(tenant, "accounts/new/"),
        {
            "bank": bank.pk,
            "account_type": "checking",
            "nickname": "Salary Account",
            "number": "1234567890",
            "currency": "USD",
            "is_active": "on",
            "opening_balance": "2500",
            "opening_date": "2026-01-05",
        },
    )
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        account = BankAccount.objects.get(nickname="Salary Account")
        assert account.gl_account is not None
        assert account.balance == 2500
        assert account.transactions.count() == 1  # the opening entry


def test_account_detail_and_add_transaction(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        bank = _bank()
        account = BankAccount.objects.create(
            bank=bank, account_type="checking", nickname="Checking",
            currency_id="USD",
        )
    client.force_login(owner)

    # detail renders
    assert client.get(_url(tenant, f"accounts/{account.pk}/")).status_code == 200

    # add a deposit
    resp = client.post(
        _url(tenant, f"accounts/{account.pk}/txns/new/"),
        {"txn_type": "deposit", "date": "2026-02-01", "amount": "300", "memo": "Gift"},
    )
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        account.refresh_from_db()
        assert account.balance == 300
        assert BankTransaction.objects.filter(account=account, txn_type="deposit").count() == 1

    body = client.get(_url(tenant, f"accounts/{account.pk}/")).content.decode()
    assert "Gift" in body and 'class="amount' in body  # register renders via c-money


def test_toggle_cleared(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        bank = _bank()
        account = BankAccount.objects.create(
            bank=bank, account_type="savings", nickname="Savings", currency_id="USD"
        )
        txn = BankTransaction.objects.create(
            account=account, txn_type="deposit", date="2026-02-01", amount=100
        )
    client.force_login(owner)
    client.post(_url(tenant, f"accounts/{account.pk}/txns/{txn.pk}/cleared/"))
    with schema_context(tenant.schema_name):
        txn.refresh_from_db()
        assert txn.cleared is True
