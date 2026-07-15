"""Loans screens end-to-end via the authenticated tenant client: dashboard/list/form/detail render,
the member gate, create-with-opening, the net-worth toggle, payments (each funding mode incl. the
co-signer external path), the balance-reconcile adjustment, the suggest-split + what-if htmx
fragments, rate changes, and the type filter."""

import datetime
from decimal import Decimal

from django_tenants.utils import schema_context

from apps.finance.models import Currency
from apps.finance.services import account_balance, net_worth
from apps.loans.models import Loan, LoanRateChange, LoanTransaction, LoanTxnType
from apps.loans.services import post_transaction
from apps.tenants.models import Membership, Role

D = Decimal
ZERO = D("0")


def _owner(make_tenant, make_user, name="Borrowdale", email="owner@example.com"):
    tenant = make_tenant(name=name)
    owner = make_user(email)
    Membership.objects.create(user=owner, tenant=tenant, role=Role.OWNER)
    return tenant, owner


def _url(tenant, path=""):
    return f"/t/{tenant.schema_name}/loans/{path}"


def _usd():
    return Currency.objects.get(code="USD")


def _loan(**kw):
    defaults = {"loan_type": "auto", "nickname": "Car loan", "currency": _usd()}
    defaults.update(kw)
    return Loan.objects.create(**defaults)


def _open(loan, amount, on=datetime.date(2026, 1, 1)):
    txn = LoanTransaction.objects.create(
        loan=loan, txn_type=LoanTxnType.OPENING, date=on, amount=D(amount)
    )
    post_transaction(txn)


def _bank_account():
    from apps.banking.models import AccountType as BAT
    from apps.banking.models import BankAccount
    from apps.banking.services import ensure_gl_account as bank_gl
    from apps.organizations.models import Organization

    acct = BankAccount.objects.create(
        bank=Organization.objects.create(name="My Bank"), account_type=BAT.CHECKING,
        nickname="Checking", currency=_usd(),
    )
    bank_gl(acct)
    return acct


# --- render + gate --------------------------------------------------------------------------

def test_dashboard_and_list_render_empty(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    assert client.get(_url(tenant)).status_code == 200
    body = client.get(_url(tenant, "all/")).content.decode()
    assert "No loans yet" in body


def test_non_member_is_denied(make_tenant, make_user, client):
    tenant, _o = _owner(make_tenant, make_user)
    outsider = make_user("outsider@example.com")
    client.force_login(outsider)
    assert client.get(_url(tenant)).status_code == 403


def test_create_form_renders(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    body = client.get(_url(tenant, "new/")).content.decode()
    assert "New loan" in body and "Counts toward my net worth" in body


# --- create -------------------------------------------------------------------------------

def test_create_mortgage_with_opening_balance(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    resp = client.post(
        _url(tenant, "new/"),
        {
            "loan_type": "mortgage", "nickname": "Home", "currency": "USD",
            "counts_toward_net_worth": "on", "is_active": "on",
            "annual_rate": "6", "term_months": "360", "payment_frequency": "monthly",
            "payment_amount": "599.55", "principal_original": "100000",
            "start_date": "2026-01-01",
            "start_mode": "opening", "opening_amount": "100000", "opening_date": "2026-01-05",
        },
    )
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        loan = Loan.objects.get(nickname="Home")
        assert loan.balance == D("100000")
        assert loan.gl_account.parent.code == "2210"  # mortgage header


def test_create_contingent_loan_off_net_worth(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    resp = client.post(
        _url(tenant, "new/"),
        {  # counts_toward_net_worth omitted → unchecked → contingent
            "loan_type": "auto", "nickname": "Son's car", "currency": "USD", "is_active": "on",
            "start_mode": "opening", "opening_amount": "15000", "opening_date": "2026-01-05",
        },
    )
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        loan = Loan.objects.get(nickname="Son's car")
        assert loan.counts_toward_net_worth is False
        assert loan.gl_account.parent.code == "2950"
        assert net_worth() == ZERO  # excluded from net worth


# --- payments -----------------------------------------------------------------------------

def test_add_payment_from_bank_clears(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        loan = _loan(principal_original=D("20000"))
        _open(loan, "20000")
        bank = _bank_account()
        loan_pk, bank_pk = loan.pk, bank.pk
    client.force_login(owner)
    client.post(
        _url(tenant, f"{loan_pk}/txns/new/"),
        {
            "txn_type": "payment", "date": "2026-02-01", "amount": "500",
            "principal": "400", "interest": "100", "escrow": "0", "fees": "0",
            "extra_principal": "0", "funding_source": "bank", "funding_account": str(bank_pk),
        },
    )
    with schema_context(tenant.schema_name):
        assert Loan.objects.get(pk=loan_pk).balance == D("19600")
        assert account_balance("5860") == D("100")
        assert account_balance("1150") == ZERO


def test_add_external_payment_by_son(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        from apps.contacts.models import Person

        son = Person.objects.create(first_name="Rohan", last_name="Shah")
        loan = _loan(principal_original=D("20000"))
        _open(loan, "20000")
        loan_pk, son_pk = loan.pk, son.pk
        interest_before = account_balance("5860")
    client.force_login(owner)
    client.post(
        _url(tenant, f"{loan_pk}/txns/new/"),
        {
            "txn_type": "payment", "date": "2026-02-01", "amount": "500",
            "principal": "450", "interest": "50", "escrow": "0", "fees": "0",
            "extra_principal": "0", "funding_source": "external", "payer_person": str(son_pk),
        },
    )
    with schema_context(tenant.schema_name):
        loan = Loan.objects.get(pk=loan_pk)
        assert loan.balance == D("19550")  # principal only
        assert account_balance("5860") == interest_before  # interest not booked
        txn = loan.transactions.get(txn_type=LoanTxnType.PAYMENT)
        assert txn.payer_person_id == son_pk


def test_balance_reconcile_adjustment(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        loan = _loan(loan_type="other", nickname="Tax plan", principal_original=D("5000"))
        _open(loan, "5000")
        loan_pk = loan.pk
    client.force_login(owner)
    client.post(
        _url(tenant, f"{loan_pk}/txns/new/"),
        {"txn_type": "adjustment", "date": "2026-03-01", "amount": "200", "direction": "decrease"},
    )
    with schema_context(tenant.schema_name):
        assert Loan.objects.get(pk=loan_pk).balance == D("4800")


# --- fragments ----------------------------------------------------------------------------

def test_payment_split_fragment(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        loan = _loan(loan_type="mortgage", annual_rate=D("6"), payment_frequency="monthly",
                     principal_original=D("10000"))
        _open(loan, "10000")
        loan_pk = loan.pk
    client.force_login(owner)
    body = client.get(
        _url(tenant, f"{loan_pk}/payment-split/") + "?amount=599.55&date=2026-02-01"
    ).content.decode()
    assert "50.00" in body and "549.55" in body  # interest / principal split


def test_what_if_payoff_fragment(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        loan = _loan(loan_type="mortgage", annual_rate=D("6"), term_months=360,
                     payment_amount=D("599.55"), payment_frequency="monthly",
                     principal_original=D("100000"))
        _open(loan, "100000")
        loan_pk = loan.pk
    client.force_login(owner)
    resp = client.get(_url(tenant, f"{loan_pk}/payoff-projection/") + "?extra=200")
    assert resp.status_code == 200
    assert "save" in resp.content.decode().lower()  # interest-saved summary


def test_rate_change_add(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        loan = _loan(loan_type="heloc", nickname="HELOC", annual_rate=D("5"),
                     rate_type="variable", credit_limit=D("50000"))
        loan_pk = loan.pk
    client.force_login(owner)
    client.post(
        _url(tenant, f"{loan_pk}/rate/"),
        {"annual_rate": "7.5", "effective_date": "2026-06-01"},
    )
    with schema_context(tenant.schema_name):
        loan = Loan.objects.get(pk=loan_pk)
        assert LoanRateChange.objects.filter(loan=loan).count() == 1
        assert loan.current_rate == D("7.5")


# --- list ---------------------------------------------------------------------------------

def test_list_type_filter(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        _loan(loan_type="mortgage", nickname="Home")
        _loan(loan_type="auto", nickname="Car")
    client.force_login(owner)
    body = client.get(_url(tenant, "all/") + "?type=mortgage").content.decode()
    assert "Home" in body and "Car" not in body


def test_detail_renders_with_schedule(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        loan = _loan(loan_type="mortgage", nickname="Home", annual_rate=D("6"), term_months=360,
                     payment_amount=D("599.55"), payment_frequency="monthly",
                     principal_original=D("100000"))
        _open(loan, "100000")
        loan_pk = loan.pk
    client.force_login(owner)
    body = client.get(_url(tenant, f"{loan_pk}/")).content.decode()
    assert "Home" in body and "Paydown" in body and "Schedule" in body
