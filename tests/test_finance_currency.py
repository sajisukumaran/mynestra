"""Full multi-currency posting: FX conversion to base, rate resolution, rounding, base balance."""

import datetime
from decimal import Decimal

import pytest
from django_tenants.utils import schema_context

from apps.finance.exceptions import MissingExchangeRate
from apps.finance.models import Currency, ExchangeRate
from apps.finance.services import (
    LineInput,
    account_raw_balance,
    post_entry,
    rate_to_base,
)
from apps.finance.services import _round_base as round_base

D = Decimal
JAN = datetime.date(2026, 1, 15)


def test_foreign_line_converts_to_base_at_explicit_rate(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        entry = post_entry(
            date=JAN,
            currency="EUR",
            lines=[
                LineInput("1110", debit=D("100"), currency="EUR", fx_rate=D("1.2")),
                LineInput("opening_balance_equity", credit=D("120")),  # base USD
            ],
        )
        line = entry.lines.get(account__code="1110")
        assert line.currency_id == "EUR" and line.debit == D("100")
        assert line.base_debit == D("120.00")  # 100 EUR × 1.2
        assert account_raw_balance("1110") == D("120.00")  # in base


def test_rate_resolved_from_exchange_rate_table(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        eur = Currency.objects.get(code="EUR")
        ExchangeRate.objects.create(currency=eur, as_of=datetime.date(2026, 1, 1), rate=D("1.10"))
        ExchangeRate.objects.create(currency=eur, as_of=datetime.date(2026, 1, 10), rate=D("1.15"))
        entry = post_entry(  # JAN=15th → latest on/before is 1.15
            date=JAN,
            lines=[
                LineInput("1110", debit=D("200"), currency="EUR"),  # no explicit rate
                LineInput("opening_balance_equity", credit=D("230")),  # 200 × 1.15
            ],
        )
        line = entry.lines.get(account__code="1110")
        assert line.fx_rate == D("1.15") and line.base_debit == D("230.00")


def test_missing_exchange_rate_raises(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        with pytest.raises(MissingExchangeRate):
            post_entry(
                date=JAN,
                lines=[
                    LineInput("1110", debit=D("50"), currency="GBP"),  # no rate, no table row
                    LineInput("opening_balance_equity", credit=D("60")),
                ],
            )


def test_rate_to_base_picks_latest_on_or_before(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        eur = Currency.objects.get(code="EUR")
        ExchangeRate.objects.create(currency=eur, as_of=datetime.date(2026, 1, 1), rate=D("1.10"))
        ExchangeRate.objects.create(currency=eur, as_of=datetime.date(2026, 2, 1), rate=D("1.20"))
        assert rate_to_base("EUR", datetime.date(2026, 1, 15)) == D("1.10")
        assert rate_to_base("EUR", datetime.date(2026, 2, 15)) == D("1.20")
        assert rate_to_base("USD", datetime.date(2026, 2, 15)) == D("1")  # base → 1


def test_base_amount_rounds_half_up(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        # 33.33 EUR × 1.005 = 33.49665 → 33.50 (HALF_UP at USD 2dp)
        entry = post_entry(
            date=JAN,
            lines=[
                LineInput("1110", debit=D("33.33"), currency="EUR", fx_rate=D("1.005")),
                LineInput("opening_balance_equity", credit=D("33.50")),
            ],
        )
        assert entry.lines.get(account__code="1110").base_debit == D("33.50")


def test_multi_currency_entry_balances_in_base(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        # An FX transfer: 96 GBP in (×1.25 = 120 base) balanced by 100 EUR out (×1.20 = 120 base).
        entry = post_entry(
            date=JAN,
            description="FX transfer",
            lines=[
                LineInput("1150", debit=D("96"), currency="GBP", fx_rate=D("1.25")),
                LineInput("1110", credit=D("100"), currency="EUR", fx_rate=D("1.20")),
            ],
        )
        assert entry.lines.count() == 2
        assert account_raw_balance("1150") == D("120.00")
        assert account_raw_balance("1110") == D("-120.00")


def test_round_base_respects_currency_precision(make_tenant):
    tenant = make_tenant()
    with schema_context(tenant.schema_name):
        jpy = Currency.objects.get(code="JPY")  # 0 dp
        usd = Currency.objects.get(code="USD")  # 2 dp
        assert round_base(D("6.5"), jpy) == D("7")  # 0dp HALF_UP
        assert round_base(D("6.725"), usd) == D("6.73")  # 2dp HALF_UP
