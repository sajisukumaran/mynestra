"""Banking screens: dashboard, accounts list, account create/detail, transaction capture, and the
member/non-member gate. Drives the authenticated tenant client end-to-end."""

import datetime
from decimal import Decimal

from django_tenants.utils import schema_context

from apps.banking.models import BankAccount, BankTransaction
from apps.contacts.models import Person
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


def test_holder_picker_shows_only_household_members(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        _bank()
        Person.objects.create(first_name="Asha", last_name="Home", is_household_member=True)
        Person.objects.create(first_name="Zubin", last_name="Mistry")  # external contact
    client.force_login(owner)
    body = client.get(_url(tenant, "accounts/new/")).content.decode()
    assert "Asha" in body       # household member is a quick-add chip
    assert "Zubin" not in body  # external contacts are reachable only via the holder search


def test_create_account_with_new_bank_inline(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)  # no banks exist yet
    resp = client.post(
        _url(tenant, "accounts/new/"),
        {
            "new_bank_name": "Brand New Bank",
            "new_branch_name": "Downtown",
            "new_bank_city": "Metropolis",
            "account_type": "checking",
            "nickname": "New Bank Checking",
            "currency": "USD",
            "is_active": "on",
        },
    )
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        bank = Organization.objects.get(name="Brand New Bank")
        assert bank.categories.filter(kind="ORG", name="Bank").exists()
        branch = bank.branches.get(name="Downtown")
        assert branch.primary_city == "Metropolis"
        acct = BankAccount.objects.get(nickname="New Bank Checking")
        assert acct.bank_id == bank.pk and acct.branch_id == branch.pk


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


def test_create_cd_with_terms_and_gl_under_1140(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        bank = _bank()
    client.force_login(owner)
    resp = client.post(
        _url(tenant, "accounts/new/"),
        {
            "bank": bank.pk,
            "account_type": "cd",
            "nickname": "12-month CD",
            "number": "5555",
            "currency": "USD",
            "is_active": "on",
            "apr": "4.5",
            "term_months": "12",
            "maturity_date": "2027-01-05",
            "opening_balance": "5000",
            "opening_date": "2026-01-05",
        },
    )
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        cd = BankAccount.objects.get(nickname="12-month CD")
        assert cd.account_type == "cd"
        assert cd.apr == Decimal("4.5") and cd.term_months == 12
        assert cd.maturity_date == datetime.date(2027, 1, 5)
        assert cd.gl_account.parent.code == "1140"  # nested under the CD header
        assert cd.balance == 5000


def test_list_cd_filter_and_count(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        bank = _bank()
        BankAccount.objects.create(
            bank=bank, account_type="checking", nickname="Everyday", currency_id="USD"
        )
        BankAccount.objects.create(
            bank=bank, account_type="cd", nickname="Nest Egg CD", currency_id="USD"
        )
    client.force_login(owner)
    body = client.get(_url(tenant, "accounts/")).content.decode()
    assert "CDs" in body  # the CD filter chip is present
    cd_only = client.get(_url(tenant, "accounts/?type=cd")).content.decode()
    assert "Nest Egg CD" in cd_only and "Everyday" not in cd_only


def test_dashboard_shows_upcoming_cd_maturity(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        bank = _bank()
        BankAccount.objects.create(
            bank=bank, account_type="cd", nickname="Maturing CD", currency_id="USD",
            maturity_date=datetime.date.today() + datetime.timedelta(days=20),
        )
    client.force_login(owner)
    body = client.get(_url(tenant)).content.decode()
    assert "Upcoming CD maturities" in body and "Maturing CD" in body


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
