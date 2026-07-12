"""Contribution tax-year attribution (IRA/HSA/529): the `tracks_contribution_year` gate, the
view capturing `tax_year` on contribution / transfer-in for eligible accounts only, prior-year
contributions, the per-year rollup, and that the tag never touches the GL."""

from decimal import Decimal

from django_tenants.utils import schema_context

from apps.finance.models import Currency
from apps.finance.services import account_balance
from apps.investments.models import InvestmentAccount, InvestmentTransaction, InvTxnType
from apps.investments.services import (
    contribution_summary,
    cost_basis,
    ensure_gl_account,
)
from apps.organizations.models import Organization
from apps.setup.models import Category
from apps.tenants.models import Membership, Role

D = Decimal


def _owner(make_tenant, make_user):
    tenant = make_tenant(name="Portfolios")
    owner = make_user("owner@example.com")
    Membership.objects.create(user=owner, tenant=tenant, role=Role.OWNER)
    return tenant, owner


def _brokerage(name="Fidelity"):
    org = Organization.objects.create(name=name)
    org.categories.add(Category.objects.get(kind="ORG", name="Brokerage"))
    return org


def _account(registration="roth_ira", nickname="My Roth"):
    acct = InvestmentAccount.objects.create(
        institution=_brokerage(), nickname=nickname, registration=registration,
        currency=Currency.objects.get(code="USD"),
    )
    ensure_gl_account(acct)
    return acct


def _url(tenant, path=""):
    return f"/t/{tenant.schema_name}/investments/{path}"


def test_tracks_contribution_year_by_registration(make_tenant):
    with schema_context(make_tenant().schema_name):
        tracked = ["traditional_ira", "roth_ira", "rollover_ira", "sep_ira",
                   "simple_ira", "hsa", "529"]
        not_tracked = ["taxable_individual", "taxable_joint", "401k", "roth_401k",
                       "403b", "457b", "custodial", "trust"]
        for reg in tracked:
            assert _account(reg, reg).tracks_contribution_year is True
        for reg in not_tracked:
            assert _account(reg, reg).tracks_contribution_year is False


def test_contribution_captures_tax_year_on_roth(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        acct = _account("roth_ira")
    client.force_login(owner)
    resp = client.post(
        _url(tenant, f"accounts/{acct.pk}/txns/new/"),
        {"txn_type": "contribution", "date": "2026-02-01", "amount": "6500",
         "tax_year": "2026"},
    )
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        txn = InvestmentTransaction.objects.get(account=acct, txn_type="contribution")
        assert txn.tax_year == 2026


def test_transfer_in_captures_tax_year(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        acct = _account("hsa", "My HSA")
    client.force_login(owner)
    resp = client.post(
        _url(tenant, f"accounts/{acct.pk}/txns/new/"),
        {"txn_type": "transfer_in", "date": "2026-02-01", "amount": "1000",
         "tax_year": "2026"},
    )
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        txn = InvestmentTransaction.objects.get(account=acct, txn_type="transfer_in")
        assert txn.tax_year == 2026


def test_prior_year_contribution_before_deadline(make_tenant, make_user, client):
    """A 2025 contribution can be made in early 2026 (before the filing deadline)."""
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        acct = _account("traditional_ira", "My IRA")
    client.force_login(owner)
    client.post(
        _url(tenant, f"accounts/{acct.pk}/txns/new/"),
        {"txn_type": "contribution", "date": "2026-03-10", "amount": "3000",
         "tax_year": "2025"},
    )
    with schema_context(tenant.schema_name):
        txn = InvestmentTransaction.objects.get(account=acct, txn_type="contribution")
        assert txn.tax_year == 2025
        assert txn.date.year == 2026  # posted in 2026, attributed to 2025


def test_taxable_account_ignores_tax_year(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        acct = _account("taxable_individual", "Taxable")
    client.force_login(owner)
    client.post(
        _url(tenant, f"accounts/{acct.pk}/txns/new/"),
        {"txn_type": "contribution", "date": "2026-02-01", "amount": "6500",
         "tax_year": "2025"},
    )
    with schema_context(tenant.schema_name):
        txn = InvestmentTransaction.objects.get(account=acct, txn_type="contribution")
        assert txn.tax_year is None  # not a year-tracked registration → tag dropped


def test_non_contribution_type_drops_tax_year(make_tenant, make_user, client):
    """A withdrawal on a tracked account never carries a tax year even if one is posted."""
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        acct = _account("roth_ira")
        InvestmentTransaction.objects.create(
            account=acct, txn_type=InvTxnType.OPENING, date="2026-01-01", amount=D("9000"),
        )
    client.force_login(owner)
    client.post(
        _url(tenant, f"accounts/{acct.pk}/txns/new/"),
        {"txn_type": "withdrawal", "date": "2026-02-01", "amount": "500", "tax_year": "2026"},
    )
    with schema_context(tenant.schema_name):
        txn = InvestmentTransaction.objects.get(account=acct, txn_type="withdrawal")
        assert txn.tax_year is None


def test_contribution_summary_groups_by_year(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account("roth_ira")
        InvestmentTransaction.objects.create(
            account=acct, txn_type=InvTxnType.CONTRIBUTION, date="2025-06-01",
            amount=D("4000"), tax_year=2025)
        InvestmentTransaction.objects.create(
            account=acct, txn_type=InvTxnType.TRANSFER_IN, date="2026-03-01",
            amount=D("2500"), tax_year=2025)  # prior-year top-up
        InvestmentTransaction.objects.create(
            account=acct, txn_type=InvTxnType.CONTRIBUTION, date="2026-05-01",
            amount=D("3000"), tax_year=2026)
        # A dividend is not a contribution — excluded even if it somehow had a year.
        InvestmentTransaction.objects.create(
            account=acct, txn_type=InvTxnType.DIVIDEND, date="2026-05-01", amount=D("50"))

        summary = contribution_summary(acct)
        assert summary == [
            {"year": 2026, "total": D("3000.0000")},
            {"year": 2025, "total": D("6500.0000")},  # 4000 + 2500, newest year first
        ]


def test_account_detail_renders_tax_year_ui(make_tenant, make_user, client):
    """The tax-year select renders for a tracked account, and a tagged contribution shows its chip
    + the by-year rollup. A 529 has no simple annual limit, so it shows the plain chips (the
    IRA/HSA limit-meter path is covered in test_investments_contribution_limits)."""
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        acct = _account("529", "Edu 529")
        InvestmentTransaction.objects.create(
            account=acct, txn_type=InvTxnType.CONTRIBUTION, date="2026-02-01",
            amount=D("6500"), tax_year=2026)
    client.force_login(owner)
    body = client.get(_url(tenant, f"accounts/{acct.pk}/")).content.decode()
    assert "Contribution tax year" in body       # form field renders
    assert "Contributions by tax year" in body   # plain by-year chips (no limit for 529)
    assert "TY 2026" in body                      # register chip


def test_account_detail_hides_tax_year_ui_for_taxable(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        acct = _account("taxable_individual", "Taxable")
    client.force_login(owner)
    body = client.get(_url(tenant, f"accounts/{acct.pk}/")).content.decode()
    assert "Contribution tax year" not in body


def test_tax_year_tag_never_touches_the_gl(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        acct = _account("roth_ira")
    client.force_login(owner)
    client.post(
        _url(tenant, f"accounts/{acct.pk}/txns/new/"),
        {"txn_type": "contribution", "date": "2026-02-01", "amount": "6500",
         "tax_year": "2026"},
    )
    with schema_context(tenant.schema_name):
        acct.refresh_from_db()
        # Invariant: gl balance == settlement cash + Σ open-lot cost, tag or no tag.
        assert account_balance(acct.gl_account) == acct.cash_balance + cost_basis(acct)
        assert acct.cash_balance == D("6500")
