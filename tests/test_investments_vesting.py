"""Phase IP2b — employer match & vesting. Covers the vesting engine (custom-tranche
`vested_fraction` + dollar/share values), the module-level overlay (funded grants are at-risk,
unfunded are upcoming, and vested value = total value minus at-risk), the guarantee that the
overlay never touches the GL, and the grant capture views. Mirrors the sibling suites' idioms."""

import datetime
from decimal import Decimal

from django_tenants.utils import schema_context

from apps.finance.models import Currency
from apps.finance.services import account_balance
from apps.investments.models import (
    InvestmentAccount,
    InvestmentTransaction,
    InvTxnType,
    Security,
    SecurityPrice,
    VestingGrant,
)
from apps.investments.services import (
    apply_transaction,
    cash_balance,
    cost_basis,
    ensure_gl_account,
    unvested_at_risk_total,
    upcoming_vesting,
    vesting_summary,
)
from apps.organizations.models import Organization
from apps.setup.models import Category
from apps.tenants.models import Membership, Role

D = Decimal
JAN = datetime.date(2026, 1, 2)


# --- Helpers (inside schema_context) ---------------------------------------------------------

def _account(nickname="Taxable", registration="taxable_individual", org=None):
    acct = InvestmentAccount.objects.create(
        institution=org or Organization.objects.create(name="Broker"),
        nickname=nickname, registration=registration, currency=Currency.objects.get(code="USD"),
    )
    ensure_gl_account(acct)
    return acct


def _fund(acct, amount="1000"):
    txn = InvestmentTransaction.objects.create(
        account=acct, txn_type=InvTxnType.OPENING, date=JAN, amount=D(amount))
    apply_transaction(txn, is_new=True)
    return txn


def _grant(acct, *, kind="dollar", total="1000", funded=True, security=None,
           grant_date=datetime.date(2022, 1, 1), tranches=()):
    g = VestingGrant.objects.create(
        account=acct, kind=kind, total=D(total), funded=funded, security=security,
        label="Grant", grant_date=grant_date)
    from apps.investments.models import VestingTranche
    for d, p in tranches:
        VestingTranche.objects.create(grant=g, vest_date=d, cumulative_percent=D(p))
    return g


# --- Vesting engine --------------------------------------------------------------------------

def test_vested_fraction_across_tranche_boundaries(make_tenant):
    with schema_context(make_tenant().schema_name):
        g = _grant(_account(), total="1000", tranches=[
            (datetime.date(2023, 1, 1), "25"),
            (datetime.date(2024, 1, 1), "50"),
            (datetime.date(2026, 1, 1), "100"),
        ])
        assert g.vested_fraction(datetime.date(2022, 6, 1)) == D("0")     # before the first tranche
        assert g.vested_fraction(datetime.date(2023, 6, 1)) == D("0.25")  # after the first
        assert g.vested_fraction(datetime.date(2024, 1, 1)) == D("0.5")   # on a tranche date
        assert g.vested_fraction(datetime.date(2030, 1, 1)) == D("1")     # past the last
        assert g.vested(datetime.date(2023, 6, 1)) == D("250")
        assert g.unvested(datetime.date(2023, 6, 1)) == D("750")


def test_shares_grant_value_uses_latest_price(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        sec = Security.objects.create(
            symbol="RSU", name="Grant Co", currency=Currency.objects.get(code="USD"))
        SecurityPrice.objects.create(security=sec, as_of=datetime.date(2025, 1, 1), price=D("10"))
        g = _grant(acct, kind="shares", total="100", funded=False, security=sec,
                   tranches=[(datetime.date(2025, 1, 1), "50"), (datetime.date(2026, 1, 1), "100")])
        as_of = datetime.date(2025, 6, 1)
        assert g.vested(as_of) == D("50")
        assert g.vested_value(as_of) == D("500")     # 50 shares × $10
        assert g.unvested_value(as_of) == D("500")


# --- Overlay ---------------------------------------------------------------------------------

def test_funded_grant_is_at_risk_and_reduces_vested_value(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        _fund(acct, "1000")  # total value = $1000 cash
        _grant(acct, total="1000", funded=True, tranches=[
            (datetime.date(2023, 1, 1), "60"), (datetime.date(2026, 1, 1), "100")])
        _rows, tot = vesting_summary(acct, datetime.date(2024, 1, 1))  # 60% vested
        assert tot["at_risk"] == D("400")        # 40% of $1000 unvested + present → forfeitable
        assert tot["upcoming"] == D("0")
        assert tot["vested_value"] == D("600")   # $1000 total value − $400 at-risk


def test_unfunded_grant_is_upcoming_not_at_risk(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        sec = Security.objects.create(
            symbol="RSU", name="Grant Co", currency=Currency.objects.get(code="USD"))
        SecurityPrice.objects.create(security=sec, as_of=datetime.date(2025, 1, 1), price=D("10"))
        _grant(acct, kind="shares", total="100", funded=False, security=sec,
               tranches=[(datetime.date(2027, 1, 1), "100")])
        as_of = datetime.date(2025, 1, 1)
        _rows, tot = vesting_summary(acct, as_of)
        assert tot["at_risk"] == D("0")
        assert tot["upcoming"] == D("1000")      # 100 shares × $10, none vested yet
        assert unvested_at_risk_total(as_of) == D("0")
        uv = upcoming_vesting(within_days=1200, as_of=as_of)
        assert len(uv) == 1 and uv[0]["unvested_value"] == D("1000")


def test_overlay_never_touches_the_gl(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        _fund(acct, "1000")
        before = account_balance(acct.gl_account)
        _grant(acct, total="500", funded=True, tranches=[
            (datetime.date(2023, 1, 1), "50"), (datetime.date(2026, 1, 1), "100")])
        acct.refresh_from_db()
        gl = account_balance(acct.gl_account)
        assert gl == before                                     # ledger unchanged by the overlay
        assert gl == cash_balance(acct) + cost_basis(acct)      # invariant intact


# --- Views -----------------------------------------------------------------------------------

def _owner(make_tenant, make_user, name="Portfolios", email="owner@example.com"):
    tenant = make_tenant(name=name)
    owner = make_user(email)
    Membership.objects.create(user=owner, tenant=tenant, role=Role.OWNER)
    return tenant, owner


def _brokerage(name="Fidelity"):
    org = Organization.objects.create(name=name)
    org.categories.add(Category.objects.get(kind="ORG", name="Brokerage"))
    return org


def _url(tenant, path=""):
    return f"/t/{tenant.schema_name}/investments/{path}"


def test_create_grant_with_tranches_via_views(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        aid = _account("Taxable", org=_brokerage()).pk
    client.force_login(owner)
    resp = client.post(_url(tenant, f"accounts/{aid}/vesting/new/"), {
        "kind": "dollar", "label": "2024 Match", "grant_date": "2024-01-01",
        "total": "6000", "funded": "on",
        "tranche_date": ["2025-01-01", "2026-01-01"], "tranche_pct": ["50", "100"]})
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        g = VestingGrant.objects.get(account_id=aid)
        assert g.total == D("6000") and g.tranches.count() == 2
        assert g.vested_fraction(datetime.date(2025, 6, 1)) == D("0.5")


def test_edit_replaces_tranches(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        acct = _account("Taxable", org=_brokerage())
        g = _grant(acct, total="1000", tranches=[
            (datetime.date(2025, 1, 1), "50"), (datetime.date(2026, 1, 1), "100")])
        aid, gid = acct.pk, g.pk
    client.force_login(owner)
    client.post(_url(tenant, f"accounts/{aid}/vesting/{gid}/edit/"), {
        "kind": "dollar", "label": "Revised", "grant_date": "2024-01-01", "total": "2000",
        "funded": "on", "tranche_date": ["2027-01-01"], "tranche_pct": ["100"]})
    with schema_context(tenant.schema_name):
        g = VestingGrant.objects.get(pk=gid)
        assert g.total == D("2000") and g.label == "Revised"
        assert g.tranches.count() == 1  # old two tranches replaced by one


def test_delete_grant_via_views(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        acct = _account("Taxable", org=_brokerage())
        gid = _grant(acct, tranches=[(datetime.date(2026, 1, 1), "100")]).pk
        aid = acct.pk
    client.force_login(owner)
    client.post(_url(tenant, f"accounts/{aid}/vesting/{gid}/delete/"), {})
    with schema_context(tenant.schema_name):
        assert VestingGrant.objects.count() == 0  # soft-deleted → excluded


def test_shares_grant_without_security_is_rejected(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        aid = _account("Taxable", org=_brokerage()).pk
    client.force_login(owner)
    client.post(_url(tenant, f"accounts/{aid}/vesting/new/"), {
        "kind": "shares", "label": "RSU", "grant_date": "2024-01-01", "total": "100",
        "tranche_date": ["2026-01-01"], "tranche_pct": ["100"]})  # no security
    with schema_context(tenant.schema_name):
        assert VestingGrant.objects.count() == 0


def test_non_monotonic_tranches_are_rejected(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        aid = _account("Taxable", org=_brokerage()).pk
    client.force_login(owner)
    client.post(_url(tenant, f"accounts/{aid}/vesting/new/"), {
        "kind": "dollar", "label": "Bad", "grant_date": "2024-01-01", "total": "1000",
        "funded": "on",
        "tranche_date": ["2025-01-01", "2026-01-01"], "tranche_pct": ["50", "25"]})  # decreasing
    with schema_context(tenant.schema_name):
        assert VestingGrant.objects.count() == 0


def test_dashboard_surfaces_unvested_at_risk(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        acct = _account("Taxable", org=_brokerage())
        _fund(acct, "1000")
        # A funded grant that fully vests only in the future → unvested (at-risk) today.
        _grant(acct, total="1000", funded=True, tranches=[(datetime.date(2030, 1, 1), "100")])
    client.force_login(owner)
    body = client.get(_url(tenant)).content.decode()
    assert "Unvested (at-risk)" in body
