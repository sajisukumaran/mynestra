"""Finance service errors — raised by the posting/balance API so callers can't write bad rows."""


class FinanceError(Exception):
    """Base class for all finance service errors."""


class UnbalancedEntry(FinanceError):
    """Σ base debits ≠ Σ base credits."""


class InvalidLine(FinanceError):
    """A line is malformed: not exactly one side, negative, non-postable/inactive account, etc."""


class EmptyEntry(FinanceError):
    """An entry has fewer than two lines."""


class ClosedPeriod(FinanceError):
    """The target fiscal period is closed (posting refused)."""


class UnknownAccount(FinanceError):
    """No account matches the given reference (code or system_key)."""


class PostedEntryImmutable(FinanceError):
    """Attempt to void/mutate a posted entry — reverse it instead."""


class MissingExchangeRate(FinanceError):
    """No exchange rate is available for a non-base currency on/before the transaction date."""
