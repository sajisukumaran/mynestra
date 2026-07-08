"""PartialDate helpers (DESIGN §5): display XX/XXXX, validation, age. Pure functions, no DB."""

import datetime

import pytest
from django.core.exceptions import ValidationError

from apps.core.partialdate import (
    PartialDate,
    format_partial_date,
    partial_date_age,
    validate_partial_date,
)


@pytest.mark.parametrize("y,m,d,expected", [
    (1974, 3, 14, "14-Mar-1974"),
    (None, 3, 14, "14-Mar-XXXX"),
    (1974, 3, None, "XX-Mar-1974"),
    (1974, None, None, "XX-XX-1974"),
    (None, None, None, ""),
    (2005, 4, 2, "02-Apr-2005"),
])
def test_format(y, m, d, expected):
    assert format_partial_date(y, m, d) == expected


def test_validate_ok():
    validate_partial_date(1974, 3, 14)
    validate_partial_date(None, 2, 29)   # leap-safe when the year is unknown
    validate_partial_date(1974, None, None)
    validate_partial_date(None, None, None)


@pytest.mark.parametrize("y,m,d", [
    (None, 13, None),   # month out of range
    (2001, 2, 29),      # not a leap year
    (None, None, 5),    # day without month
    (1974, 4, 31),      # April has 30 days
])
def test_validate_bad(y, m, d):
    with pytest.raises(ValidationError):
        validate_partial_date(y, m, d)


def test_age():
    on = datetime.date(2025, 7, 8)
    assert partial_date_age(1974, 3, 14, on) == 51
    assert partial_date_age(1974, 12, 31, on) == 50   # birthday not yet reached this year
    assert partial_date_age(None, 3, 14, on) is None   # unknown year → no age


def test_value_object():
    pd = PartialDate(1974, 3, 14)
    assert pd.is_set
    assert pd.display == "14-Mar-1974"
    assert pd.age == partial_date_age(1974, 3, 14)
    assert not PartialDate().is_set
    assert PartialDate(None, 3, 14).display == "14-Mar-XXXX"
