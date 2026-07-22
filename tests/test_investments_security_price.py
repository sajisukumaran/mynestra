"""Correcting a mistaken manually-entered security price: edit (value / date / source), delete, and
editing onto a date another mark already holds (overwrite + drop). Prices are pure market-value
marks — no GL / lot effect."""

import datetime
from decimal import Decimal

from django_tenants.utils import schema_context

from apps.finance.models import Currency
from apps.investments.models import (
    InvestmentAccount,
    InvestmentTransaction,
    InvTxnType,
    Security,
    SecurityPrice,
)
from apps.investments.services import apply_transaction, ensure_gl_account
from apps.organizations.models import Organization
from apps.setup.models import Category
from apps.tenants.models import Membership, Role

D = Decimal


def _owner(make_tenant, make_user):
    tenant = make_tenant(name="Portfolios")
    owner = make_user("owner@example.com")
    Membership.objects.create(user=owner, tenant=tenant, role=Role.OWNER)
    return tenant, owner


def _url(tenant, path=""):
    return f"/t/{tenant.schema_name}/investments/{path}"


def _sec():
    return Security.objects.create(
        symbol="AAPL", name="Apple", currency=Currency.objects.get(code="USD"))


def test_edit_price_corrects_value_date_and_source(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        sec = _sec()
        p = SecurityPrice.objects.create(
            security=sec, as_of=datetime.date(2026, 7, 10), price=D("999"))
    client.force_login(owner)
    resp = client.post(
        _url(tenant, f"securities/{sec.pk}/price/{p.pk}/edit/"),
        {"price": "214.30", "as_of": "2026-07-09", "source": "corrected"},
    )
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        p.refresh_from_db()
        assert p.price == D("214.30")
        assert p.as_of == datetime.date(2026, 7, 9)
        assert p.source == "corrected"


def test_delete_price(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        sec = _sec()
        p = SecurityPrice.objects.create(
            security=sec, as_of=datetime.date(2026, 7, 10), price=D("999"))
    client.force_login(owner)
    resp = client.post(_url(tenant, f"securities/{sec.pk}/price/{p.pk}/delete/"))
    assert resp.status_code == 302
    with schema_context(tenant.schema_name):
        assert not SecurityPrice.objects.filter(pk=p.pk).exists()


def test_edit_onto_existing_date_overwrites_and_drops_row(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        sec = _sec()
        keep = SecurityPrice.objects.create(
            security=sec, as_of=datetime.date(2026, 7, 9), price=D("100"))
        dup = SecurityPrice.objects.create(
            security=sec, as_of=datetime.date(2026, 7, 10), price=D("200"))
    client.force_login(owner)
    # Move `dup` onto the 9th, which `keep` already holds → overwrite keep, delete dup.
    client.post(
        _url(tenant, f"securities/{sec.pk}/price/{dup.pk}/edit/"),
        {"price": "150", "as_of": "2026-07-09", "source": ""},
    )
    with schema_context(tenant.schema_name):
        assert SecurityPrice.objects.filter(security=sec).count() == 1
        keep.refresh_from_db()
        assert keep.as_of == datetime.date(2026, 7, 9)
        assert keep.price == D("150")  # overwritten with the edited value
        assert not SecurityPrice.objects.filter(pk=dup.pk).exists()


def test_mass_price_update_creates_filled_skips_blank_and_excludes_non_quotable(
    make_tenant, make_user, client
):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        usd = Currency.objects.get(code="USD")
        a = Security.objects.create(symbol="AAA", name="Alpha", currency=usd)
        b = Security.objects.create(symbol="BBB", name="Beta", currency=usd)
        cd = Security.objects.create(symbol="", name="6mo CD", kind="cd", currency=usd)
        aid, bid, cdid = a.pk, b.pk, cd.pk
    client.force_login(owner)
    # GET lists the quotable stocks and excludes the CD.
    body = client.get(_url(tenant, "securities/mass-prices/")).content.decode()
    assert "AAA" in body and "BBB" in body and "6mo CD" not in body
    # POST: A filled, B blank, CD posted-but-excluded. Follow to the list — the success toast
    # renders there (also proves the securities list template still renders with its new bits).
    resp = client.post(_url(tenant, "securities/mass-prices/"), {
        "as_of": "2026-01-15", "source": "Yahoo",
        f"price_{aid}": "42.62", f"price_{bid}": "", f"price_{cdid}": "99"}, follow=True)
    assert resp.status_code == 200
    assert "Updated 1 price as of 15 Jan 2026" in resp.content.decode()
    with schema_context(tenant.schema_name):
        pa = SecurityPrice.objects.get(security_id=aid)
        assert pa.as_of == datetime.date(2026, 1, 15)
        assert pa.price == D("42.62") and pa.source == "Yahoo"
        assert not SecurityPrice.objects.filter(security_id=bid).exists()   # blank → skipped
        assert not SecurityPrice.objects.filter(security_id=cdid).exists()  # excluded → ignored


def test_mass_price_update_requires_as_of_date(make_tenant, make_user, client):
    """The as-of date is required and does NOT default to today — prices here are usually
    back-dated, so a missing date must not be silently stamped with the current date. GET renders
    the field blank; a dateless POST writes nothing and re-renders with an error toast."""
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        sec = _sec()
        sid = sec.pk
    client.force_login(owner)
    # GET: the as-of field renders empty (no today default) so the correct date must be entered.
    body = client.get(_url(tenant, "securities/mass-prices/")).content.decode()
    assert 'name="as_of" value=""' in body.replace("\n", " ")
    # POST without a date: nothing written, error surfaced, no silent stamp on today.
    resp = client.post(_url(tenant, "securities/mass-prices/"), {
        "as_of": "", "source": "Yahoo", f"price_{sid}": "42.62"})
    assert resp.status_code == 200
    assert "Enter the as-of date" in resp.content.decode()
    with schema_context(tenant.schema_name):
        assert not SecurityPrice.objects.filter(security_id=sid).exists()


def test_mass_price_update_tolerates_pasted_formatting(make_tenant, make_user, client):
    """A price copied with a thousands-separator comma / currency symbol (e.g. "$1,234.56") is
    stripped and stored, not silently skipped."""
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        usd = Currency.objects.get(code="USD")
        a = Security.objects.create(symbol="AAA", name="Alpha", currency=usd)
        b = Security.objects.create(symbol="BBB", name="Beta", currency=usd)
        aid, bid = a.pk, b.pk
    client.force_login(owner)
    resp = client.post(_url(tenant, "securities/mass-prices/"), {
        "as_of": "2026-01-15", "source": "",
        f"price_{aid}": "1,234.56", f"price_{bid}": "$2,000"}, follow=True)
    assert resp.status_code == 200
    assert "Updated 2 prices as of 15 Jan 2026" in resp.content.decode()
    with schema_context(tenant.schema_name):
        assert SecurityPrice.objects.get(security_id=aid).price == D("1234.56")
        assert SecurityPrice.objects.get(security_id=bid).price == D("2000")


def test_mass_price_update_overwrites_existing_mark_on_that_date(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        sec = _sec()
        SecurityPrice.objects.create(security=sec, as_of=datetime.date(2026, 1, 15), price=D("10"))
        sid = sec.pk
    client.force_login(owner)
    client.post(_url(tenant, "securities/mass-prices/"), {
        "as_of": "2026-01-15", "source": "fix", f"price_{sid}": "12.50"})
    with schema_context(tenant.schema_name):
        marks = SecurityPrice.objects.filter(security_id=sid, as_of=datetime.date(2026, 1, 15))
        assert marks.count() == 1                       # overwrote, no duplicate
        assert marks.first().price == D("12.50")


def test_mass_price_scoped_to_account_lists_only_held_quotables(make_tenant, make_user, client):
    """?account=<pk> narrows the list to just the quotable instruments that account holds — held
    stocks appear, un-held stocks and the account's non-quotable positions don't — and a save lands
    back on the account detail page (not the securities list)."""
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        usd = Currency.objects.get(code="USD")
        org = Organization.objects.create(name="Fidelity")
        org.categories.add(Category.objects.get(kind="ORG", name="Brokerage"))
        acct = InvestmentAccount.objects.create(
            institution=org, nickname="Taxable", registration="taxable_individual", currency=usd)
        ensure_gl_account(acct)
        held = Security.objects.create(symbol="AAA", name="Alpha", currency=usd)
        unheld = Security.objects.create(symbol="BBB", name="Beta", currency=usd)
        # Buy the held stock so the account has an open lot in it; leave `unheld` untransacted.
        buy = InvestmentTransaction.objects.create(
            account=acct, txn_type=InvTxnType.BUY, date=datetime.date(2026, 1, 2),
            security=held, quantity=D("10"), price=D("10"), amount=D("100"), fee=D("0"))
        apply_transaction(buy, is_new=True)
        aid, bid, apk = held.pk, unheld.pk, acct.pk
    client.force_login(owner)
    # GET scoped to the account: the held stock is listed, the un-held one isn't.
    body = client.get(_url(tenant, f"securities/mass-prices/?account={apk}")).content.decode()
    assert "AAA" in body and "BBB" not in body
    assert f'name="account" value="{apk}"' in body  # scope carried into the POST
    # POST (scoped) records the price and redirects back to the account page.
    resp = client.post(_url(tenant, "securities/mass-prices/"), {
        "account": str(apk), "as_of": "2026-01-15", "source": "Yahoo", f"price_{aid}": "42.62"})
    assert resp.status_code == 302
    assert resp.url == f"/t/{tenant.schema_name}/investments/accounts/{apk}/"
    with schema_context(tenant.schema_name):
        assert SecurityPrice.objects.get(security_id=aid).price == D("42.62")
        assert not SecurityPrice.objects.filter(security_id=bid).exists()


def test_security_detail_renders_price_edit_control(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        sec = _sec()
        p = SecurityPrice.objects.create(
            security=sec, as_of=datetime.date(2026, 7, 10), price=D("214.30"))
    client.force_login(owner)
    body = client.get(_url(tenant, f"securities/{sec.pk}/")).content.decode()
    assert "Edit price" in body
    assert f"price/{p.pk}/edit/" in body
    assert f"price/{p.pk}/delete/" in body
