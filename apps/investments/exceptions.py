"""Investment-service errors (raised by the lot engine / posting layer)."""


class InvestmentError(Exception):
    """Base for investment service errors."""


class InsufficientShares(InvestmentError):
    """A sell (or return of capital) tried to draw more shares/basis than the account holds."""
