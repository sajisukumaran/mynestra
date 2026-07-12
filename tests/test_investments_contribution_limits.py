"""Contribution-limit tracking: per-person aggregate 'used' against the shared annual IRS limit,
age-based catch-up from the holder's birth year, HSA self/family coverage, the over-limit flag, and
the no-limit fallthrough for SEP/SIMPLE/529/taxable. Pure module metadata — no GL."""

import datetime
from decimal import Decimal

from django_tenants.utils import schema_context

from apps.contacts.models import Person
from apps.finance.models import Currency
from apps.investments.models import (
    HsaCoverage,
    InvestmentAccount,
    InvestmentAccountHolder,
    InvestmentTransaction,
    InvTxnType,
)
from apps.investments.services import contribution_limit_status, ensure_gl_account
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


def _person(first="Jane", dob_year=1980):
    return Person.objects.create(
        first_name=first, last_name="Doe", is_household_member=True, dob_year=dob_year)


def _account(reg, nickname, person=None, coverage=HsaCoverage.SELF_ONLY):
    acct = InvestmentAccount.objects.create(
        institution=_brokerage(nickname), nickname=nickname, registration=reg,
        currency=Currency.objects.get(code="USD"), hsa_coverage=coverage)
    ensure_gl_account(acct)
    if person:
        InvestmentAccountHolder.objects.create(account=acct, person=person, is_primary=True)
    return acct


def _contrib(acct, year, amount, ttype=InvTxnType.CONTRIBUTION):
    return InvestmentTransaction.objects.create(
        account=acct, txn_type=ttype, date=datetime.date(year, 6, 1),
        amount=D(amount), tax_year=year)


def _row(status, year):
    return next(r for r in status["rows"] if r["year"] == year)


def _url(tenant, path=""):
    return f"/t/{tenant.schema_name}/investments/{path}"


def test_ira_limit_aggregates_across_a_persons_iras(make_tenant):
    with schema_context(make_tenant().schema_name):
        jane = _person(dob_year=1980)  # age 45 in 2025 — no catch-up
        roth = _account("roth_ira", "Roth", jane)
        trad = _account("traditional_ira", "Trad", jane)
        _contrib(roth, 2025, "4000")
        _contrib(trad, 2025, "2500")

        status = contribution_limit_status(roth, as_of=datetime.date(2025, 12, 1))
        row = _row(status, 2025)
        assert status["category_label"] == "IRA"
        assert status["account_count"] == 2
        assert row["used"] == D("6500")          # aggregate across both IRAs
        assert row["limit"] == D("7000")         # 2025 base, no catch-up
        assert row["remaining"] == D("500")
        assert row["over"] is False
        assert row["this_account"] == D("4000")  # just this Roth, for the note


def test_ira_catch_up_applies_from_age_50(make_tenant):
    with schema_context(make_tenant().schema_name):
        bob = _person("Bob", dob_year=1970)  # age 55 in 2025 → catch-up eligible
        ira = _account("roth_ira", "Roth", bob)
        _contrib(ira, 2025, "8000")
        row = _row(contribution_limit_status(ira, as_of=datetime.date(2025, 12, 1)), 2025)
        assert row["catch_up"] == D("1000")
        assert row["limit"] == D("8000")  # 7000 + 1000 catch-up
        assert row["over"] is False


def test_hsa_family_coverage_uses_family_limit(make_tenant):
    with schema_context(make_tenant().schema_name):
        jane = _person(dob_year=1990)
        hsa = _account("hsa", "HSA", jane, coverage=HsaCoverage.FAMILY)
        _contrib(hsa, 2025, "5000")
        status = contribution_limit_status(hsa, as_of=datetime.date(2025, 12, 1))
        row = _row(status, 2025)
        assert status["category_label"] == "HSA"
        assert status["coverage_label"] == "Family"
        assert row["limit"] == D("8550")  # 2025 HSA family


def test_hsa_self_only_is_the_default_limit(make_tenant):
    with schema_context(make_tenant().schema_name):
        jane = _person(dob_year=1990)
        hsa = _account("hsa", "HSA", jane)  # default self-only
        _contrib(hsa, 2025, "2000")
        row = _row(contribution_limit_status(hsa, as_of=datetime.date(2025, 12, 1)), 2025)
        assert row["limit"] == D("4300")  # 2025 HSA self-only


def test_over_limit_flag_and_over_by(make_tenant):
    with schema_context(make_tenant().schema_name):
        jane = _person(dob_year=1990)
        ira = _account("roth_ira", "Roth", jane)
        _contrib(ira, 2025, "9000")
        row = _row(contribution_limit_status(ira, as_of=datetime.date(2025, 12, 1)), 2025)
        assert row["over"] is True
        assert row["over_by"] == D("2000")  # 9000 − 7000
        assert row["remaining"] == D("0")
        assert row["pct"] == 100  # bar clamps at 100%


def test_no_limit_status_for_uncapped_registrations(make_tenant):
    with schema_context(make_tenant().schema_name):
        assert contribution_limit_status(_account("taxable_individual", "Tax")) is None
        assert contribution_limit_status(_account("sep_ira", "SEP")) is None
        assert contribution_limit_status(_account("529", "Edu")) is None


def test_year_without_a_table_entry_has_no_limit(make_tenant):
    with schema_context(make_tenant().schema_name):
        jane = _person(dob_year=1990)
        ira = _account("roth_ira", "Roth", jane)
        _contrib(ira, 2020, "3000")  # 2020 predates the table
        row = _row(contribution_limit_status(ira, as_of=datetime.date(2020, 12, 1)), 2020)
        assert row["limit"] is None
        assert row["used"] == D("3000")


def test_current_year_row_shown_even_with_no_contributions(make_tenant):
    with schema_context(make_tenant().schema_name):
        jane = _person(dob_year=1990)
        ira = _account("roth_ira", "Roth", jane)
        status = contribution_limit_status(ira, as_of=datetime.date(2026, 3, 1))
        row = _row(status, 2026)  # current year present → shows headroom
        assert row["used"] == D("0")
        assert row["limit"] == D("7500")  # 2026 IRA base


def test_transfer_in_counts_toward_the_limit(make_tenant):
    with schema_context(make_tenant().schema_name):
        jane = _person(dob_year=1990)
        ira = _account("roth_ira", "Roth", jane)
        _contrib(ira, 2025, "3000", ttype=InvTxnType.CONTRIBUTION)
        _contrib(ira, 2025, "1000", ttype=InvTxnType.TRANSFER_IN)  # funding from bank
        row = _row(contribution_limit_status(ira, as_of=datetime.date(2025, 12, 1)), 2025)
        assert row["used"] == D("4000")


def test_account_detail_renders_limit_meter(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        jane = _person(dob_year=1990)
        ira = _account("roth_ira", "Roth", jane)
        _contrib(ira, 2025, "4000")
    client.force_login(owner)
    body = client.get(_url(tenant, f"accounts/{ira.pk}/")).content.decode()
    assert "contribution limits" in body
    assert "meter-track" in body


def test_account_create_saves_hsa_coverage(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    client.force_login(owner)
    resp = client.post(
        _url(tenant, "accounts/new/"),
        {
            "new_institution_name": "HealthBank",
            "registration": "hsa",
            "nickname": "Family HSA",
            "currency": "USD",
            "is_active": "on",
            "hsa_coverage": "family",
        },
    )
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        acct = InvestmentAccount.objects.get(nickname="Family HSA")
        assert acct.hsa_coverage == "family"
