"""Phase IP6 — portfolio value-over-time overlay. Covers the non-mutating as-of-date holdings
reconstruction (MemLotStore replay), carry-forward pricing, the two-line series (market + invested),
the SVG geometry precompute, the range windows, and the guarantee that the overlay never touches the
GL. `_inv` (gl == cash + Σ open cost) still holds; the KEY guardrail is that as-of-today reconciles
to the live read models. Mirrors the sibling suites' idioms."""

import datetime
from decimal import Decimal

from django_tenants.utils import schema_context

from apps.finance.models import Currency, JournalEntry
from apps.finance.services import account_balance
from apps.investments.models import (
    InvestmentAccount,
    InvestmentTransaction,
    InvTxnType,
    Lot,
    Security,
    SecurityPrice,
)
from apps.investments.services import (
    PriceCarry,
    apply_transaction,
    cash_balance,
    cost_basis,
    dashboard_stats,
    ensure_gl_account,
    holdings,
    line_chart_points,
    positions_as_of,
    price_as_of,
    value_events_on,
    value_over_time,
)
from apps.organizations.models import Organization
from apps.setup.models import Category
from apps.tenants.models import Membership, Role

D = Decimal
JAN = datetime.date(2026, 1, 2)
FEB = datetime.date(2026, 2, 2)
MAR = datetime.date(2026, 3, 2)
APR = datetime.date(2026, 4, 2)
TODAY = datetime.date(2026, 6, 1)


def _account(nickname="Taxable", org=None):
    acct = InvestmentAccount.objects.create(
        institution=org or Organization.objects.create(name="Broker"),
        nickname=nickname, registration="taxable_individual",
        currency=Currency.objects.get(code="USD"),
    )
    ensure_gl_account(acct)
    return acct


def _sec(symbol, name=None):
    return Security.objects.create(
        symbol=symbol, name=name or symbol, currency=Currency.objects.get(code="USD")
    )


def _add(acct, ttype, date, *, security=None, qty="0", price="0", amount="0", fee="0", **extra):
    txn = InvestmentTransaction.objects.create(
        account=acct, txn_type=ttype, date=date, security=security,
        quantity=D(qty), price=D(price), amount=D(amount), fee=D(fee), **extra,
    )
    apply_transaction(txn, is_new=True)
    txn.refresh_from_db()
    return txn


def _price(security, on, value):
    SecurityPrice.objects.create(security=security, as_of=on, price=D(value))


# --- Reconstruction equivalence (the guardrail) ----------------------------------------------

def test_positions_as_of_today_matches_live_holdings(make_tenant):
    """The MemLotStore replay, evaluated at today, reproduces the live holdings() + cash exactly —
    this is what makes the market line trustworthy without a parallel implementation."""
    with schema_context(make_tenant().schema_name):
        acct = _account()
        a, b = _sec("AAA"), _sec("BBB")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        _add(acct, InvTxnType.BUY, FEB, security=a, qty="10", price="50", amount="500")
        _add(acct, InvTxnType.BUY, FEB, security=b, qty="20", price="10", amount="200")
        _add(acct, InvTxnType.SELL, MAR, security=a, qty="4", price="60", amount="240")
        _add(acct, InvTxnType.SPLIT, MAR, security=b,
             split_ratio_new=D("2"), split_ratio_old=D("1"))

        cash, pos = positions_as_of(acct, TODAY)
        assert cash == cash_balance(acct)
        live = {h.security.pk: (h.quantity, h.cost_basis) for h in holdings(acct)}
        assert pos == live


def test_positions_reconstruct_through_merger_and_short(make_tenant):
    """As-of-today reconstruction is exact through corporate actions + shorts (same engine)."""
    with schema_context(make_tenant().schema_name):
        acct = _account()
        x, y, z = _sec("XXX"), _sec("YYY"), _sec("ZZZ")
        _add(acct, InvTxnType.OPENING, JAN, amount="20000")
        _add(acct, InvTxnType.BUY, FEB, security=x, qty="10", price="50", amount="500")
        _add(acct, InvTxnType.MERGER, MAR, security=x, target_security=y,
             split_ratio_new=D("2"), split_ratio_old=D("1"))
        _add(acct, InvTxnType.SELL_SHORT, MAR, security=z, qty="100", price="7", amount="700")

        _cash, pos = positions_as_of(acct, TODAY)
        live = {h.security.pk: (h.quantity, h.cost_basis) for h in holdings(acct)}
        assert pos == live
        assert pos[y.pk] == (D("20"), D("500"))       # X merged into Y, basis carried
        assert pos[z.pk] == (D("-100"), D("-700"))    # short surfaces as a negative position
        assert x.pk not in pos                        # X fully consumed by the merger


def test_positions_as_of_past_date_predates_later_buys(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        a = _sec("AAA")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        _add(acct, InvTxnType.BUY, FEB, security=a, qty="10", price="50", amount="500")
        _add(acct, InvTxnType.BUY, APR, security=a, qty="10", price="60", amount="600")

        # As of March, only the February buy has happened.
        cash, pos = positions_as_of(acct, MAR)
        assert pos[a.pk] == (D("10"), D("500"))
        assert cash == D("9500")                      # 10000 − 500


# --- Carry-forward pricing -------------------------------------------------------------------

def test_price_as_of_carries_forward(make_tenant):
    with schema_context(make_tenant().schema_name):
        a = _sec("AAA")
        _price(a, datetime.date(2026, 1, 10), "50")
        _price(a, datetime.date(2026, 3, 15), "70")
        assert price_as_of(a, datetime.date(2026, 1, 5)) is None      # before any price
        assert price_as_of(a, datetime.date(2026, 1, 10)) == D("50")  # exact
        assert price_as_of(a, datetime.date(2026, 2, 1)) == D("50")   # carry forward
        assert price_as_of(a, datetime.date(2026, 3, 15)) == D("70")
        assert price_as_of(a, datetime.date(2026, 6, 1)) == D("70")   # carry forward latest


def test_price_carry_batch_matches_price_as_of(make_tenant):
    with schema_context(make_tenant().schema_name):
        a, b = _sec("AAA"), _sec("BBB")
        _price(a, datetime.date(2026, 1, 10), "50")
        _price(a, datetime.date(2026, 3, 15), "70")
        _price(b, datetime.date(2026, 2, 1), "12")
        carry = PriceCarry([a.pk, b.pk])
        for d in [datetime.date(2026, 1, 5), datetime.date(2026, 2, 1), datetime.date(2026, 4, 1)]:
            assert carry.price_at(a.pk, d) == price_as_of(a, d)
            assert carry.price_at(b.pk, d) == price_as_of(b, d)
        assert carry.price_at(999, datetime.date(2026, 4, 1)) is None  # unknown security


# --- The overlay never touches the GL --------------------------------------------------------

def test_reconstruction_posts_nothing(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        a = _sec("AAA")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        _add(acct, InvTxnType.BUY, FEB, security=a, qty="10", price="50", amount="500")
        gl_before = account_balance(acct.gl_account)
        entries_before = JournalEntry.objects.count()
        lots_before = Lot.objects.count()

        positions_as_of(acct, TODAY)
        positions_as_of(acct, MAR)

        assert account_balance(acct.gl_account) == gl_before
        assert JournalEntry.objects.count() == entries_before
        assert Lot.objects.count() == lots_before      # no lots created/deleted
        assert cost_basis(acct) == D("500")


# --- The series (value_over_time) ------------------------------------------------------------

def test_series_reconciles_to_dashboard_at_today(make_tenant):
    """The KEY guardrail: the last series point equals the live dashboard figures."""
    with schema_context(make_tenant().schema_name):
        acct = _account()
        a, b = _sec("AAA"), _sec("BBB")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        _add(acct, InvTxnType.BUY, FEB, security=a, qty="10", price="50", amount="500")
        _add(acct, InvTxnType.BUY, FEB, security=b, qty="20", price="10", amount="200")
        _price(a, MAR, "70")
        _price(b, MAR, "8")

        vot = value_over_time("ALL", today=TODAY)
        stats = dashboard_stats()
        assert vot["last_market"] == stats["total_value"]
        assert vot["last_invested"] == stats["total_cash"] + stats["total_cost"]
        assert vot["last_invested"] == account_balance(acct.gl_account)  # cash + cost, from the GL
        assert vot["gain"] == stats["unrealized"]
        assert vot["series"][-1][0] == TODAY
        assert vot["series"][0][0] == vot["start"]           # anchored at the range start


def test_invested_reconciles_to_gl_at_interior_dates(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        a = _sec("AAA")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        _add(acct, InvTxnType.BUY, FEB, security=a, qty="10", price="50", amount="500")
        _add(acct, InvTxnType.SELL, MAR, security=a, qty="4", price="60", amount="240")
        _price(a, FEB, "55")

        vot = value_over_time("ALL", today=TODAY)
        for d, invested, _market in vot["series"]:
            # invested line (cash + cost) matches the GL's as-of balance at every event date
            assert invested == account_balance(acct.gl_account, as_of=d)


def test_market_uses_carry_forward_price_else_cost(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        a = _sec("AAA")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        _add(acct, InvTxnType.BUY, FEB, security=a, qty="10", price="50", amount="500")
        _price(a, MAR, "70")

        vot = value_over_time("ALL", today=TODAY)
        by_date = {d: (inv, mkt) for d, inv, mkt in vot["series"]}
        # Before any price: market == invested (unpriced holding falls back to cost).
        assert by_date[FEB][0] == by_date[FEB][1]
        # After the price: market reflects 10 × 70 = 700 vs 500 cost → +200 over invested.
        assert by_date[MAR][1] - by_date[MAR][0] == D("200")


def test_range_window_carries_pre_window_state(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        a = _sec("AAA")
        # Activity well before a 3M window ending at TODAY (2026-06-01 → window starts ~Mar 3).
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        _add(acct, InvTxnType.BUY, FEB, security=a, qty="10", price="50", amount="500")

        vot = value_over_time("3M", today=TODAY)
        first_date, first_invested, _m = vot["series"][0]
        assert first_date == vot["start"]                 # anchored at window start
        assert first_invested == D("10000")               # pre-window state carried, not zeroed
        assert vot["series"][-1][0] == TODAY


def test_empty_portfolio_single_flat_point(make_tenant):
    with schema_context(make_tenant().schema_name):
        vot = value_over_time("1Y", today=TODAY)
        assert vot["series"] == [(TODAY, D("0"), D("0"))]
        assert vot["min"] == D("0") and vot["max"] == D("0")
        geo = line_chart_points(vot["series"], min_v=vot["min"], max_v=vot["max"],
                                start=vot["start"], end=vot["end"])
        assert geo["gain_area_d"].endswith("Z")           # valid, no divide-by-zero
        assert len(geo["points"]) == 1


# --- Geometry (line_chart_points) ------------------------------------------------------------

def test_geometry_is_well_formed(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        a = _sec("AAA")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        _add(acct, InvTxnType.BUY, FEB, security=a, qty="10", price="50", amount="500")
        _price(a, MAR, "70")
        _price(a, APR, "40")
        vot = value_over_time("ALL", today=TODAY)
        geo = line_chart_points(vot["series"], min_v=vot["min"], max_v=vot["max"],
                                start=vot["start"], end=vot["end"])

        xs = [p["x"] for p in geo["points"]]
        assert xs == sorted(xs)                           # x monotonically ascending
        for p in geo["points"]:
            assert 0 <= p["y_inv"] <= geo["height"]
            assert 0 <= p["y_mkt"] <= geo["height"]
        assert geo["gain_area_d"].startswith("M ") and geo["gain_area_d"].endswith("Z")
        assert geo["market_points"] and geo["invested_points"]
        assert len(geo["y_ticks"]) == 3 and len(geo["x_ticks"]) == 3


# --- Range windows: new presets + custom from/to ---------------------------------------------

def test_named_range_windows_anchor_at_the_right_start(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        a = _sec("AAA")
        _add(acct, InvTxnType.OPENING, datetime.date(2019, 1, 2), amount="10000")
        _add(acct, InvTxnType.BUY, datetime.date(2019, 2, 2), security=a,
             qty="10", price="50", amount="500")

        expected = {
            "3M": TODAY - datetime.timedelta(days=90),
            "6M": TODAY - datetime.timedelta(days=182),
            "1Y": TODAY - datetime.timedelta(days=365),
            "3Y": TODAY - datetime.timedelta(days=1096),
            "5Y": TODAY - datetime.timedelta(days=1826),
            "YTD": datetime.date(TODAY.year, 1, 1),
        }
        for key, start in expected.items():
            vot = value_over_time(key, today=TODAY)
            assert vot["start"] == start, key
            assert vot["end"] == TODAY, key
            assert vot["series"][0][0] == start, key       # anchored at the window start
            assert vot["series"][-1][0] == TODAY, key


def test_invalid_range_falls_back_to_1y(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        _add(acct, InvTxnType.OPENING, datetime.date(2020, 1, 2), amount="10000")
        vot = value_over_time("BOGUS", today=TODAY)
        assert vot["range"] == "1Y"
        assert vot["start"] == TODAY - datetime.timedelta(days=365)


def test_custom_window_is_honored(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        a = _sec("AAA")
        _add(acct, InvTxnType.OPENING, datetime.date(2025, 1, 2), amount="10000")
        _add(acct, InvTxnType.BUY, datetime.date(2025, 3, 2), security=a,
             qty="10", price="50", amount="500")
        _price(a, datetime.date(2025, 4, 2), "70")

        start, end = datetime.date(2025, 2, 1), datetime.date(2025, 5, 1)  # end < TODAY on purpose
        vot = value_over_time("1Y", today=TODAY, window_start=start, window_end=end)
        assert vot["start"] == start
        assert vot["end"] == end
        assert vot["series"][0][0] == start
        assert vot["series"][-1][0] == end                 # ends at the custom end, not today


def test_custom_end_before_start_collapses_cleanly(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        _add(acct, InvTxnType.OPENING, datetime.date(2025, 1, 2), amount="10000")
        vot = value_over_time("ALL", today=TODAY,
                              window_start=datetime.date(2025, 6, 1),
                              window_end=datetime.date(2025, 3, 1))
        assert vot["start"] == vot["end"] == datetime.date(2025, 3, 1)
        assert len(vot["series"]) == 1                      # no crash, single clean point


# --- Click-to-drill events (value_events_on) -------------------------------------------------

def test_value_events_on_returns_txns_and_priced_impact(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        a = _sec("AAA")
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        _add(acct, InvTxnType.BUY, FEB, security=a, qty="10", price="50", amount="500")
        _price(a, FEB, "50")                                 # prior mark
        _price(a, MAR, "200")                                # spike: 10 × (200 − 50) = +1500
        div = _add(acct, InvTxnType.DIVIDEND, MAR, security=a, amount="12")

        ev = value_events_on(MAR)
        assert ev["date"] == MAR
        assert div in ev["txns"]
        assert len(ev["prices"]) == 1
        row = ev["prices"][0]
        assert row["security"] == a
        assert row["price"] == D("200")
        assert row["prev"] == D("50")
        assert row["delta"] == D("150")
        assert row["impact"] == D("1500")


def test_value_events_on_skips_unheld_and_posts_nothing(make_tenant):
    with schema_context(make_tenant().schema_name):
        acct = _account()
        a, b = _sec("AAA"), _sec("BBB")                      # b is never held
        _add(acct, InvTxnType.OPENING, JAN, amount="10000")
        _add(acct, InvTxnType.BUY, FEB, security=a, qty="10", price="50", amount="500")
        _price(b, MAR, "99")                                 # a mark for an unheld security
        gl_before = account_balance(acct.gl_account)
        entries_before = JournalEntry.objects.count()
        lots_before = Lot.objects.count()

        ev = value_events_on(MAR)
        assert ev["prices"] == []                            # b isn't held → not shown
        assert account_balance(acct.gl_account) == gl_before
        assert JournalEntry.objects.count() == entries_before
        assert Lot.objects.count() == lots_before


# --- Dashboard card + htmx range fragment (the view uses the real clock) ----------------------

def _owner(make_tenant, make_user, name="Bourse", email="owner@example.com"):
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


def _seed_history(org):
    """A small priced portfolio dated relative to today (view uses the real clock)."""
    t = datetime.date.today()
    acct = _account(org=org)
    a = _sec("AAA")
    _add(acct, InvTxnType.OPENING, t - datetime.timedelta(days=200), amount="10000")
    _add(acct, InvTxnType.BUY, t - datetime.timedelta(days=150), security=a,
         qty="10", price="50", amount="500")
    _price(a, t - datetime.timedelta(days=100), "70")
    return acct


def test_dashboard_renders_value_chart(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        _seed_history(_brokerage())
    client.force_login(owner)
    resp = client.get(_url(tenant, ""))
    assert resp.status_code == 200
    body = resp.content.decode()
    assert 'id="value-chart"' in body
    assert "chart-svg" in body                         # chart rendered (>1 point in the 1Y window)
    assert "value-over-time/?range=3M" in body         # range toggle wired


def test_value_over_time_fragment_switches_range(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        _seed_history(_brokerage())
    client.force_login(owner)
    for rng in ("3M", "6M", "YTD", "1Y", "3Y", "5Y", "ALL"):
        resp = client.get(_url(tenant, "value-over-time/") + f"?range={rng}")
        assert resp.status_code == 200
        assert 'id="value-chart"' in resp.content.decode()


def test_value_over_time_fragment_custom_window(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        _seed_history(_brokerage())
    client.force_login(owner)
    t = datetime.date.today()
    start = (t - datetime.timedelta(days=120)).isoformat()
    end = (t - datetime.timedelta(days=10)).isoformat()
    resp = client.get(_url(tenant, "value-over-time/") + f"?start={start}&end={end}")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert 'id="value-chart"' in body
    assert start in body and end in body                 # custom inputs prefilled via x-data


def test_dashboard_chart_carries_hover_payload(make_tenant, make_user, client):
    import html as html_
    import json as json_
    import re

    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        _seed_history(_brokerage())
    client.force_login(owner)
    body = client.get(_url(tenant, "")).content.decode()
    assert "chart-plot" in body                          # interactive overlay is present
    m = re.search(r'data-points="([^"]*)"', body)
    assert m
    pts = json_.loads(html_.unescape(m.group(1)))
    assert len(pts) >= 2
    assert [p["x"] for p in pts] == sorted(p["x"] for p in pts)   # x ascending
    for p in pts:
        assert set(p) >= {"x", "ym", "yi", "iso", "d", "m", "i", "g"}
    assert pts[0]["m"].startswith("$")                   # preformatted money string


def test_value_events_fragment_lists_the_day(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        _seed_history(_brokerage())
    client.force_login(owner)
    buy_date = (datetime.date.today() - datetime.timedelta(days=150)).isoformat()
    resp = client.get(_url(tenant, "value-events/") + f"?on={buy_date}")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Activity on" in body
    assert "AAA" in body                                 # the buy of AAA that day


def test_value_events_fragment_blank_without_a_date(make_tenant, make_user, client):
    tenant, owner = _owner(make_tenant, make_user)
    with schema_context(tenant.schema_name):
        _seed_history(_brokerage())
    client.force_login(owner)
    resp = client.get(_url(tenant, "value-events/"))
    assert resp.status_code == 200
    assert resp.content.decode().strip() == ""
