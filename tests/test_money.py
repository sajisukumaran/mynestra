"""Unit tests for the money formatting filters used by the c-money component."""

from decimal import Decimal

from apps.core.templatetags.money import money_sign, moneyfmt


def test_moneyfmt_thousands_grouping():
    assert moneyfmt(Decimal("1234.5"), "2") == "1,234.50"


def test_moneyfmt_zero_decimals():
    assert moneyfmt(Decimal("150000"), "0") == "150,000"


def test_moneyfmt_indian_grouping():
    assert moneyfmt(Decimal("1234567.5"), "2:indian") == "12,34,567.50"


def test_moneyfmt_plain_grouping():
    assert moneyfmt(Decimal("1234.5"), "2:plain") == "1234.50"


def test_moneyfmt_returns_absolute_value():
    assert moneyfmt(Decimal("-89.99"), "2") == "89.99"  # sign handled by the component


def test_moneyfmt_invalid_input():
    assert moneyfmt("not-a-number") == ""


def test_money_sign_classifies():
    assert money_sign(Decimal("-1")) == "neg"
    assert money_sign(0) == "zero"
    assert money_sign("5") == "pos"
    assert money_sign(None) == "pos"
