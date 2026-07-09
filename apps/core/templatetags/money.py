"""Money formatting filters used by the `c-money` component (and any template with a Decimal).

`moneyfmt` returns the *absolute* value grouped for display (sign is handled by the component, which
uses accounting-style parentheses for negatives); `money_sign` classifies the value so the component
can colour it. Grouping honours the household `number_format` (plain / thousands / indian).
"""

import re
from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()


def _to_decimal(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _group(digits: str, grouping: str) -> str:
    if grouping == "plain":
        return digits
    if grouping == "indian":
        if len(digits) <= 3:
            return digits
        head, tail = digits[:-3], digits[-3:]
        head = re.sub(r"(?<=\d)(?=(?:\d\d)+$)", ",", head)
        return f"{head},{tail}"
    return f"{int(digits):,}"  # thousands


@register.filter
def moneyfmt(value, spec="2") -> str:
    """Format magnitude. `spec` = "<dp>" or "<dp>:<grouping>" (grouping default thousands)."""
    parts = str(spec).split(":")
    try:
        places = int(parts[0])
    except (ValueError, IndexError):
        places = 2
    grouping = parts[1] if len(parts) > 1 else "thousands"

    amount = _to_decimal(value)
    if amount is None:
        return ""
    amount = abs(amount).quantize(Decimal(1).scaleb(-places))
    text = f"{amount:.{places}f}"
    int_part, _, frac = text.partition(".")
    grouped = _group(int_part, grouping)
    return f"{grouped}.{frac}" if places else grouped


@register.filter
def money_sign(value) -> str:
    """'neg' | 'zero' | 'pos' — lets the component colour/parenthesise the amount."""
    amount = _to_decimal(value)
    if amount is None:
        return "pos"
    if amount < 0:
        return "neg"
    if amount == 0:
        return "zero"
    return "pos"
