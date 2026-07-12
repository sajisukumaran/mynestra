"""End-of-day price providers for the `fetch_eod_prices` command.

A provider is just `fetch(symbol) -> PriceQuote | None`. The active one is chosen by
`settings.INVESTMENTS_PRICE_PROVIDER` (env `PRICE_PROVIDER`); keyed providers read
`settings.INVESTMENTS_PRICE_API_KEY` (env `PRICE_API_KEY`). Adding a source is a few lines + a
`PROVIDERS` entry. Manual price entry (the SecurityPrice UI) always works alongside auto-fetch —
both just write `SecurityPrice` rows.

Shipped providers:
  - stooq        keyless CSV, no signup (default) — US + many global exchanges
  - alphavantage keyed JSON (GLOBAL_QUOTE)
  - finnhub      keyed JSON (/quote)
  - yfinance     keyless Python library (optional dependency; unofficial Yahoo data)

HTTP goes through `_http_get` so tests monkeypatch one seam; parsers are pure functions.
"""

from __future__ import annotations

import csv
import datetime
import io
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.conf import settings


@dataclass(frozen=True)
class PriceQuote:
    as_of: datetime.date
    price: Decimal


class PriceError(Exception):
    """A provider is misconfigured (missing key / unknown name / missing dependency)."""


def _http_get(url: str, *, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "MyNestra/1.0 (+price-fetch)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed https hosts)
        return resp.read().decode("utf-8", "replace")


def _dec(value) -> Decimal | None:
    """A positive Decimal, or None for blanks / 'N/D' / zero / garbage."""
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, AttributeError):
        return None
    return d if d > 0 else None


def _isodate(value) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat((value or "").strip())
    except (ValueError, TypeError):
        return None


def _require_key(name: str) -> str:
    key = settings.INVESTMENTS_PRICE_API_KEY
    if not key:
        raise PriceError(f"price provider '{name}' needs PRICE_API_KEY to be set")
    return key


# --- Stooq (keyless EOD CSV) -----------------------------------------------------------------

def _stooq_symbol(symbol: str) -> str:
    s = symbol.strip().lower()
    return s if "." in s else f"{s}.us"  # a bare ticker defaults to the US market


def parse_stooq_csv(text: str) -> PriceQuote | None:
    """Last data row of Stooq's `Date,Open,High,Low,Close,Volume` CSV → its close. Stooq answers a
    bad symbol with a body of 'N/D', which yields no rows → None."""
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        return None
    last = rows[-1]
    price = _dec(last.get("Close"))
    as_of = _isodate(last.get("Date"))
    return PriceQuote(as_of, price) if (price and as_of) else None


def stooq_fetch(symbol: str) -> PriceQuote | None:
    url = f"https://stooq.com/q/d/l/?s={urllib.parse.quote(_stooq_symbol(symbol))}&i=d"
    return parse_stooq_csv(_http_get(url))


# --- Alpha Vantage (keyed, GLOBAL_QUOTE) -----------------------------------------------------

def parse_alphavantage(text: str) -> PriceQuote | None:
    quote = (json.loads(text) or {}).get("Global Quote") or {}
    price = _dec(quote.get("05. price"))
    as_of = _isodate(quote.get("07. latest trading day")) or datetime.date.today()
    return PriceQuote(as_of, price) if price else None


def alphavantage_fetch(symbol: str) -> PriceQuote | None:
    key = _require_key("alphavantage")
    url = (
        "https://www.alphavantage.co/query?function=GLOBAL_QUOTE"
        f"&symbol={urllib.parse.quote(symbol)}&apikey={urllib.parse.quote(key)}"
    )
    return parse_alphavantage(_http_get(url))


# --- Finnhub (keyed, /quote) -----------------------------------------------------------------

def parse_finnhub(text: str) -> PriceQuote | None:
    data = json.loads(text) or {}
    price = _dec(data.get("c"))  # current price ≈ the close once the market has shut
    ts = data.get("t")
    as_of = (
        datetime.datetime.fromtimestamp(int(ts), tz=datetime.UTC).date()
        if ts else datetime.date.today()
    )
    return PriceQuote(as_of, price) if price else None


def finnhub_fetch(symbol: str) -> PriceQuote | None:
    key = _require_key("finnhub")
    url = (
        f"https://finnhub.io/api/v1/quote?symbol={urllib.parse.quote(symbol)}"
        f"&token={urllib.parse.quote(key)}"
    )
    return parse_finnhub(_http_get(url))


# --- yfinance (keyless library; optional dependency) -----------------------------------------

def yfinance_fetch(symbol: str) -> PriceQuote | None:
    try:
        import yfinance
    except ImportError as exc:  # pragma: no cover - depends on the optional dep
        raise PriceError(
            "provider 'yfinance' needs the yfinance package (`uv add yfinance`) — "
            "or pick another PRICE_PROVIDER"
        ) from exc
    hist = yfinance.Ticker(symbol).history(period="5d")
    if hist is None or hist.empty:
        return None
    price = _dec(hist["Close"].iloc[-1])
    as_of = hist.index[-1].date()
    return PriceQuote(as_of, price) if price else None


PROVIDERS = {
    "stooq": stooq_fetch,
    "alphavantage": alphavantage_fetch,
    "finnhub": finnhub_fetch,
    "yfinance": yfinance_fetch,
}


def get_provider(name: str | None = None):
    """The active provider callable. `name` overrides the configured default."""
    name = (name or settings.INVESTMENTS_PRICE_PROVIDER or "stooq").strip().lower()
    try:
        return PROVIDERS[name]
    except KeyError:
        raise PriceError(
            f"unknown price provider '{name}'; choose from {sorted(PROVIDERS)}"
        ) from None
