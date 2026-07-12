"""EOD price auto-fetch: the provider parsers (pure), provider selection, and the tenant-aware
`fetch_eod_prices` command — which upserts SecurityPrice for auto-tracked securities only and is
idempotent. Network is mocked at the single `pricing._http_get` seam; no real HTTP in tests."""

import datetime
from decimal import Decimal

import pytest
from django.core.management import call_command
from django_tenants.utils import schema_context

from apps.finance.models import Currency
from apps.investments import pricing
from apps.investments.management.commands.price_cron import next_run
from apps.investments.models import Security

D = Decimal

STOOQ_CSV = (
    "Date,Open,High,Low,Close,Volume\n"
    "2026-07-09,210.00,215.00,209.50,213.10,50000000\n"
    "2026-07-10,213.50,216.00,212.80,214.30,48000000\n"
)


def _usd():
    return Currency.objects.get(code="USD")


# --- Provider parsers (pure) -----------------------------------------------------------------

def test_parse_stooq_csv_takes_last_close():
    q = pricing.parse_stooq_csv(STOOQ_CSV)
    assert q.as_of == datetime.date(2026, 7, 10)
    assert q.price == D("214.30")


def test_parse_stooq_csv_handles_bad_symbol():
    assert pricing.parse_stooq_csv("N/D\n") is None
    assert pricing.parse_stooq_csv("") is None


def test_parse_alphavantage():
    text = ('{"Global Quote": {"01. symbol": "AAPL", "05. price": "214.3000", '
            '"07. latest trading day": "2026-07-10"}}')
    q = pricing.parse_alphavantage(text)
    assert q.price == D("214.3000")
    assert q.as_of == datetime.date(2026, 7, 10)


def test_parse_finnhub():
    q = pricing.parse_finnhub('{"c": 214.30, "t": 1752192000}')
    assert q.price == D("214.30")
    assert isinstance(q.as_of, datetime.date)


def test_get_provider_selection_and_unknown():
    assert pricing.get_provider("stooq") is pricing.stooq_fetch
    assert pricing.get_provider(None) is pricing.stooq_fetch  # settings default
    with pytest.raises(pricing.PriceError):
        pricing.get_provider("nope")


def test_keyed_provider_without_key_raises(settings):
    settings.INVESTMENTS_PRICE_API_KEY = ""
    with pytest.raises(pricing.PriceError):
        pricing.alphavantage_fetch("AAPL")


# --- Scheduler next-run logic (pure) ---------------------------------------------------------

def _first_weekday(start):
    d = start
    while d.weekday() >= 5:
        d += datetime.timedelta(days=1)
    return d


def test_next_run_same_day_when_time_is_ahead():
    d = _first_weekday(datetime.datetime(2026, 7, 6, 9, 0))
    nxt = next_run(d, 23, 10)
    assert nxt.date() == d.date()  # later the same weekday
    assert (nxt.hour, nxt.minute, nxt.second) == (23, 10, 0)


def test_next_run_rolls_forward_when_time_passed():
    d = _first_weekday(datetime.datetime(2026, 7, 6, 9, 0)).replace(hour=23, minute=30)
    nxt = next_run(d, 23, 10)
    assert nxt > d
    assert nxt.weekday() < 5
    assert nxt.date() > d.date()


def test_next_run_skips_the_weekend():
    saturday = datetime.datetime(2026, 7, 6, 12, 0)
    while saturday.weekday() != 5:
        saturday += datetime.timedelta(days=1)
    nxt = next_run(saturday, 23, 10)
    assert nxt.weekday() == 0  # Monday


# --- The command (DB + mocked HTTP) ----------------------------------------------------------

def _seed(kinds_only=False):
    usd = _usd()
    aapl = Security.objects.create(symbol="AAPL", name="Apple", currency=usd, track_price=True)
    mmf = Security.objects.create(symbol="SPAXX", name="MM", kind="money_market",
                                  currency=usd, track_price=True)          # skipped: MMF
    cd = Security.objects.create(symbol="CD1", name="CD", kind="cd", currency=usd,
                                 track_price=True)                         # skipped: CD
    off = Security.objects.create(symbol="MSFT", name="Microsoft", currency=usd,
                                  track_price=False)                        # skipped: flag off
    nosym = Security.objects.create(symbol="", name="Bespoke", currency=usd, track_price=True)
    return aapl, mmf, cd, off, nosym


def test_command_fetches_and_upserts_only_tracked(make_tenant, monkeypatch):
    monkeypatch.setattr(pricing, "_http_get", lambda url, **kw: STOOQ_CSV)
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        aapl, mmf, cd, off, nosym = _seed()
    call_command("fetch_eod_prices", schema=tenant.schema_name)
    with schema_context(tenant.schema_name):
        assert aapl.prices.count() == 1
        p = aapl.prices.first()
        assert p.price == D("214.30")
        assert p.as_of == datetime.date(2026, 7, 10)
        assert p.source == "auto:stooq"
        for skipped in (mmf, cd, off, nosym):
            assert skipped.prices.count() == 0


def test_command_is_idempotent(make_tenant, monkeypatch):
    monkeypatch.setattr(pricing, "_http_get", lambda url, **kw: STOOQ_CSV)
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        Security.objects.create(symbol="AAPL", name="Apple", currency=_usd(), track_price=True)
    call_command("fetch_eod_prices", schema=tenant.schema_name)
    call_command("fetch_eod_prices", schema=tenant.schema_name)  # re-run same day
    with schema_context(tenant.schema_name):
        sec = Security.objects.get(symbol="AAPL")
        assert sec.prices.count() == 1  # one row, not two


def test_command_dry_run_writes_nothing(make_tenant, monkeypatch):
    monkeypatch.setattr(pricing, "_http_get", lambda url, **kw: STOOQ_CSV)
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        Security.objects.create(symbol="AAPL", name="Apple", currency=_usd(), track_price=True)
    call_command("fetch_eod_prices", schema=tenant.schema_name, dry_run=True)
    with schema_context(tenant.schema_name):
        assert Security.objects.get(symbol="AAPL").prices.count() == 0


def test_command_survives_a_bad_symbol(make_tenant, monkeypatch):
    def fake_get(url, **kw):
        return STOOQ_CSV if "aapl" in url.lower() else "N/D\n"  # ZZZZ returns no data
    monkeypatch.setattr(pricing, "_http_get", fake_get)
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        Security.objects.create(symbol="AAPL", name="Apple", currency=_usd(), track_price=True)
        Security.objects.create(symbol="ZZZZ", name="Bad", currency=_usd(), track_price=True)
    call_command("fetch_eod_prices", schema=tenant.schema_name)  # must not raise
    with schema_context(tenant.schema_name):
        assert Security.objects.get(symbol="AAPL").prices.count() == 1
        assert Security.objects.get(symbol="ZZZZ").prices.count() == 0
