"""Cards UI: dashboard/list render, member gating, credit-card create (holders, inline issuer,
opening balance), charge/payment flows, the Expert Accounting tab, debit-card CRUD + the
banking withdrawal debit-card tag."""

from decimal import Decimal

from django.db import connection
from django_tenants.utils import schema_context

from apps.banking.models import BankAccount
from apps.cards.models import CardTxnType, CreditCard, CreditCardTransaction, DebitCard
from apps.contacts.models import Person
from apps.organizations.models import Organization
from apps.setup.models import Category
from apps.tenants.models import Membership, Role, Tenant

D = Decimal


def _owner(make_tenant, make_user, name="Acme", email="owner@example.com"):
    tenant = make_tenant(name=name)
    owner = make_user(email)
    Membership.objects.create(user=owner, tenant=tenant, role=Role.OWNER)
    return tenant, owner


def _url(tenant, path=""):
    return f"/t/{tenant.schema_name}/cards/{path}"


def _issuer():
    org = Organization.objects.create(name="HDFC Bank")
    org.categories.add(Category.objects.get(kind="ORG", name="Bank"))
    return org


def _expert(tenant):
    connection.set_schema_to_public()
    Tenant.objects.filter(pk=tenant.pk).update(accounting_mode="expert")


# --- Render + gating ------------------------------------------------------------------------

def test_dashboard_and_lists_render(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    for path in ("", "credit/", "credit/new/", "debit/", "debit/new/"):
        assert client.get(_url(tenant, path)).status_code == 200


def test_non_member_forbidden(make_tenant, make_user, client):
    tenant, _o = _owner(make_tenant, make_user)
    outsider = make_user("outsider@example.com")
    client.force_login(outsider)
    assert client.get(_url(tenant)).status_code == 403


# --- Credit-card create ---------------------------------------------------------------------

def test_create_credit_card_with_opening_balance(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        bank = _issuer()
    client.force_login(owner)
    resp = client.post(_url(tenant, "credit/new/"), {
        "issuer": bank.pk, "nickname": "Amex Gold", "network": "amex", "currency": "USD",
        "credit_limit": "5000", "is_active": "on",
        "opening_balance": "1200", "opening_date": "2026-01-05",
    })
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        card = CreditCard.objects.get(nickname="Amex Gold")
        assert card.gl_account_id is not None
        assert card.balance == D("1200")
        assert card.transactions.filter(txn_type=CardTxnType.OPENING).exists()


def test_inline_issuer_creation(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    resp = client.post(_url(tenant, "credit/new/"), {
        "new_issuer_name": "Citi", "nickname": "Citi Rewards", "network": "visa",
        "currency": "USD", "is_active": "on",
    })
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        card = CreditCard.objects.get(nickname="Citi Rewards")
        assert card.issuer.name == "Citi"
        assert card.issuer.categories.filter(kind="ORG", name="Bank").exists()


def test_holder_picker_shows_household_only(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        Person.objects.create(first_name="Raj", last_name="Sharma", is_household_member=True)
        Person.objects.create(first_name="Zoe", last_name="External", is_household_member=False)
    client.force_login(owner)
    body = client.get(_url(tenant, "credit/new/")).content.decode()
    assert "Raj Sharma" in body        # household member = a quick chip
    assert "Zoe External" not in body   # external contacts only via search


# --- Charge / payment flows -----------------------------------------------------------------

def _card(tenant):
    with schema_context(tenant.schema_name):
        from apps.cards.services import ensure_gl_account
        from apps.finance.models import Currency

        card = CreditCard.objects.create(
            issuer=_issuer(), nickname="Card", network="visa",
            currency=Currency.objects.get(code="USD"), credit_limit=D("2000"),
        )
        ensure_gl_account(card)
        return card.pk


def test_add_charge_flow(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    pk = _card(tenant)
    client.force_login(owner)
    resp = client.post(_url(tenant, f"credit/{pk}/txns/new/"), {
        "txn_type": "charge", "amount": "75", "date": "2026-02-01",
    })
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        card = CreditCard.objects.get(pk=pk)
        assert card.balance == D("75")


def test_payment_from_bank_auto_matches(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    pk = _card(tenant)
    with schema_context(tenant.schema_name):
        from apps.banking.services import ensure_gl_account as ensure_bank_gl
        from apps.finance.models import Account
        from apps.finance.services import account_balance

        acct = BankAccount.objects.create(
            bank=Organization.objects.create(name="Bank2"), account_type="checking",
            nickname="Chk", currency_id="USD",
        )
        ensure_bank_gl(acct)
        # seed a balance owed to pay down
        CreditCardTransaction.objects.create(
            card_id=pk, txn_type=CardTxnType.OPENING, date="2026-01-01", amount=D("500")
        )
        from apps.cards.services import post_transaction

        post_transaction(CreditCard.objects.get(pk=pk).transactions.first())
        acct_pk = acct.pk
    client.force_login(owner)
    resp = client.post(_url(tenant, f"credit/{pk}/txns/new/"), {
        "txn_type": "payment", "amount": "300", "date": "2026-02-02",
        "counter_account": acct_pk, "auto_match": "on",
    })
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        assert CreditCard.objects.get(pk=pk).balance == D("200")  # 500 - 300
        clearing = Account.objects.get(system_key="transfer_clearing")
        assert account_balance(clearing) == D("0")  # auto-matched leg nets clearing


# --- Expert Accounting tab ------------------------------------------------------------------

def test_accounting_tab_visibility(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    std = client.get(_url(tenant, "credit/new/")).content.decode()
    assert 'name="map_charge_category"' not in std
    _expert(tenant)
    body = client.get(_url(tenant, "credit/new/")).content.decode()
    assert 'name="map_charge_category"' in body and 'name="gl_mode"' in body


# --- Debit cards ----------------------------------------------------------------------------

def test_debit_card_crud_and_tagging(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        acct = BankAccount.objects.create(
            bank=Organization.objects.create(name="Bank3"), account_type="checking",
            nickname="Chk", currency_id="USD",
        )
        acct_pk = acct.pk
    client.force_login(owner)
    # create
    resp = client.post(_url(tenant, "debit/new/"), {
        "bank_account": acct_pk, "nickname": "Everyday", "network": "visa",
        "number": "4000123499990001", "is_active": "on",
    })
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        card = DebitCard.objects.get(nickname="Everyday")
        dc_pk = card.pk
    assert client.get(_url(tenant, f"debit/{dc_pk}/")).status_code == 200

    # tag a bank withdrawal with the debit card
    client.post(f"/t/{tenant.schema_name}/banking/accounts/{acct_pk}/txns/new/", {
        "txn_type": "withdrawal", "amount": "40", "date": "2026-02-01", "card": dc_pk,
    })
    with schema_context(tenant.schema_name):
        assert DebitCard.objects.get(pk=dc_pk).bank_txns.count() == 1
