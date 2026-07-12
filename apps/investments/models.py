"""Investments — investment accounts, their securities/holdings (per-lot cost basis) and a
transaction register. Module 5, the third consumer of the finance general-ledger backbone.

Design (locked with the user):
- **Cost basis in the GL; market value in the module.** Each `InvestmentAccount` owns one postable
  `finance.Account` (nested under the `1210`/`1220`/`1230` group header matching its tax
  registration). The GL carries the account **at cost** (cash + cost basis of open lots). Current
  market value / unrealized gain are computed here from manually-entered `SecurityPrice`s and are
  never posted to the GL.
- **Per-security tax lots.** Every buy creates a `Lot`; sells consume lots (FIFO or specific) via
  `LotConsumption`, so realized gains are tax-accurate and reversible (see the services module).
- **Explicit settlement cash.** Money in/out, buys, sells, dividends, interest and fees all move an
  internal cash balance (`cash_balance`), computed from the register (`signed_cash`).

Invariant (asserted in tests): `account_balance(gl) == cash_balance + Σ open-lot cost_basis`.

Soft-deletable + audited like every tenant model (DESIGN §5).
"""

import datetime
from decimal import Decimal

from django.db import models
from simple_history.models import HistoricalRecords

from apps.core.models import SoftDeleteModel, TimeStampedModel
from apps.core.partialdate import PartialDate
from apps.finance.models import AMOUNT_DECIMALS, AMOUNT_MAX_DIGITS, ZERO

# Quantities (fractional shares) and per-unit prices carry more precision than money amounts.
QTY_MAX_DIGITS = 20
QTY_DECIMALS = 6
PRICE_MAX_DIGITS = 20
PRICE_DECIMALS = 6


def _amount(**kw):
    return models.DecimalField(
        max_digits=AMOUNT_MAX_DIGITS, decimal_places=AMOUNT_DECIMALS, **kw
    )


def _qty(**kw):
    return models.DecimalField(max_digits=QTY_MAX_DIGITS, decimal_places=QTY_DECIMALS, **kw)


def _price(**kw):
    return models.DecimalField(max_digits=PRICE_MAX_DIGITS, decimal_places=PRICE_DECIMALS, **kw)


# --- Security master -------------------------------------------------------------------------

class SecurityKind(models.TextChoices):
    STOCK = "stock", "Stock"
    ETF = "etf", "ETF"
    MUTUAL_FUND = "mutual_fund", "Mutual fund"
    BOND = "bond", "Bond"
    CD = "cd", "CD / Term deposit"
    MONEY_MARKET = "money_market", "Money market"
    OPTION = "option", "Option"
    OTHER = "other", "Other"


class AssetClass(models.TextChoices):
    EQUITY = "equity", "Equity"
    FIXED_INCOME = "fixed_income", "Fixed income"
    CASH = "cash", "Cash & equivalents"
    REAL_ASSET = "real_asset", "Real assets"
    DERIVATIVE = "derivative", "Derivatives"
    OTHER = "other", "Other"


class OptionRight(models.TextChoices):
    CALL = "call", "Call"
    PUT = "put", "Put"


# Chip tint per asset class (drives the allocation donut / bars). From the curated chip set.
ASSET_CLASS_TINT = {
    AssetClass.EQUITY: "teal",
    AssetClass.FIXED_INCOME: "blue",
    AssetClass.CASH: "slate",
    AssetClass.REAL_ASSET: "amber",
    AssetClass.DERIVATIVE: "rose",
    AssetClass.OTHER: "violet",
}


class Security(SoftDeleteModel):
    """A tradable instrument the household holds — a stock, ETF, fund, bond, CD, etc. Prices are
    entered by hand (`SecurityPrice`); the latest one marks holdings to market."""

    symbol = models.CharField(max_length=20, blank=True)  # ticker; blank for CDs / bespoke holdings
    name = models.CharField(max_length=160)
    kind = models.CharField(max_length=14, choices=SecurityKind.choices, default=SecurityKind.STOCK)
    asset_class = models.CharField(
        max_length=14, choices=AssetClass.choices, default=AssetClass.EQUITY
    )
    currency = models.ForeignKey("finance.Currency", on_delete=models.PROTECT, related_name="+")

    # CD / term-deposit attributes (only meaningful when kind == CD).
    apr = models.DecimalField(max_digits=7, decimal_places=4, null=True, blank=True)  # e.g. 5.25 %
    maturity_date = models.DateField(null=True, blank=True)

    # Option-contract attributes (only meaningful when kind == OPTION). The underlying is the equity
    # the option is written on; the multiplier is shares controlled per contract (usually 100).
    underlying = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="option_contracts"
    )
    option_right = models.CharField(
        max_length=4, choices=OptionRight.choices, blank=True
    )
    strike = _price(null=True, blank=True)                 # strike price, per underlying share
    expiration = models.DateField(null=True, blank=True)
    multiplier = _qty(default=Decimal("100"))              # underlying shares per contract

    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["symbol", "name"]

    def __str__(self) -> str:
        return self.display

    @property
    def display(self) -> str:
        if self.kind == SecurityKind.OPTION and self.underlying_id:
            return self.option_display
        if self.symbol:
            return f"{self.symbol} · {self.name}" if self.name else self.symbol
        return self.name

    @property
    def is_option(self) -> bool:
        return self.kind == SecurityKind.OPTION

    @property
    def option_display(self) -> str:
        """A compact contract label, e.g. `AAPL 250C 19-Jul` (underlying, strike, right, expiry)."""
        und = self.underlying.symbol if self.underlying_id else (self.symbol or "?")
        strike = f"{self.strike:g}" if self.strike is not None else "?"
        right = "C" if self.option_right == OptionRight.CALL else (
            "P" if self.option_right == OptionRight.PUT else "")
        exp = self.expiration.strftime("%d-%b") if self.expiration else ""
        return f"{und} {strike}{right} {exp}".strip()

    def contracts_of(self, quantity) -> Decimal:
        """Convert a shares-equivalent quantity back to a contract count for display."""
        mult = self.multiplier or Decimal("1")
        return (quantity / mult) if mult else quantity

    @property
    def kind_label(self) -> str:
        return self.get_kind_display()

    @property
    def asset_class_label(self) -> str:
        return self.get_asset_class_display()

    @property
    def tint(self) -> str:
        return ASSET_CLASS_TINT.get(self.asset_class, "slate")

    @property
    def is_cd(self) -> bool:
        return self.kind == SecurityKind.CD

    @property
    def latest_price(self):
        """The most recent manually-entered price, or None if none recorded."""
        row = self.prices.order_by("-as_of").first()
        return row.price if row else None


class SecurityPrice(TimeStampedModel):
    """A dated mark for a security (units of the security's currency per share/unit). Mirrors
    `finance.ExchangeRate`: the latest on/before a date is the mark. Manually entered."""

    security = models.ForeignKey(Security, on_delete=models.CASCADE, related_name="prices")
    as_of = models.DateField()
    price = _price()
    source = models.CharField(max_length=60, blank=True)

    class Meta:
        ordering = ["-as_of"]
        constraints = [
            models.UniqueConstraint(fields=["security", "as_of"], name="securityprice_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.security} {self.price} @ {self.as_of}"


# --- Investment account ----------------------------------------------------------------------

class Registration(models.TextChoices):
    TAXABLE_INDIVIDUAL = "taxable_individual", "Taxable — Individual"
    TAXABLE_JOINT = "taxable_joint", "Taxable — Joint"
    TRADITIONAL_IRA = "traditional_ira", "Traditional IRA"
    ROTH_IRA = "roth_ira", "Roth IRA"
    ROLLOVER_IRA = "rollover_ira", "Rollover IRA"
    SEP_IRA = "sep_ira", "SEP IRA"
    SIMPLE_IRA = "simple_ira", "SIMPLE IRA"
    P401K = "401k", "401(k)"
    ROTH_401K = "roth_401k", "Roth 401(k)"
    P403B = "403b", "403(b)"
    P457B = "457b", "457(b)"
    HSA = "hsa", "HSA"
    ESA_529 = "529", "529 plan"
    CUSTODIAL = "custodial", "Custodial (UGMA/UTMA)"
    TRUST = "trust", "Trust"
    OTHER = "other", "Other"


class AccountGroup(models.TextChoices):
    TAXABLE = "taxable", "Taxable"
    RETIREMENT = "retirement", "Retirement"
    HSA = "hsa", "HSA"


# Registration → GL group header (drives which of 1210/1220/1230 the account's node nests under).
REGISTRATION_GROUP = {
    Registration.TAXABLE_INDIVIDUAL: AccountGroup.TAXABLE,
    Registration.TAXABLE_JOINT: AccountGroup.TAXABLE,
    Registration.TRADITIONAL_IRA: AccountGroup.RETIREMENT,
    Registration.ROTH_IRA: AccountGroup.RETIREMENT,
    Registration.ROLLOVER_IRA: AccountGroup.RETIREMENT,
    Registration.SEP_IRA: AccountGroup.RETIREMENT,
    Registration.SIMPLE_IRA: AccountGroup.RETIREMENT,
    Registration.P401K: AccountGroup.RETIREMENT,
    Registration.ROTH_401K: AccountGroup.RETIREMENT,
    Registration.P403B: AccountGroup.RETIREMENT,
    Registration.P457B: AccountGroup.RETIREMENT,
    Registration.HSA: AccountGroup.HSA,
    Registration.ESA_529: AccountGroup.TAXABLE,
    Registration.CUSTODIAL: AccountGroup.TAXABLE,
    Registration.TRUST: AccountGroup.TAXABLE,
    Registration.OTHER: AccountGroup.TAXABLE,
}

# GL group header system_keys, per AccountGroup (seeded in finance COA / migration 0006).
GROUP_HEADER_KEY = {
    AccountGroup.TAXABLE: "brokerage",
    AccountGroup.RETIREMENT: "retirement",
    AccountGroup.HSA: "hsa",
}

GROUP_TINT = {
    AccountGroup.TAXABLE: "teal",
    AccountGroup.RETIREMENT: "violet",
    AccountGroup.HSA: "emerald",
}

# Registrations whose contributions are attributed to a specific tax year — the IRS lets you make a
# prior-year contribution up to the filing deadline (e.g. a 2025 IRA/HSA contribution in early
# 2026). 401(k)/403(b)/457(b) are payroll, calendar-year (no prior-year mechanism) and taxable/
# custodial/trust have no annual-contribution concept, so neither carries a tax year.
CONTRIBUTION_YEAR_REGISTRATIONS = frozenset({
    Registration.TRADITIONAL_IRA,
    Registration.ROTH_IRA,
    Registration.ROLLOVER_IRA,
    Registration.SEP_IRA,
    Registration.SIMPLE_IRA,
    Registration.HSA,
    Registration.ESA_529,
})


class InvestmentAccount(SoftDeleteModel):
    """A household investment account at an institution (Fidelity, Vanguard, …). Its balance lives
    in the GL via `gl_account` (at cost); this row carries the human-facing metadata and the
    module-computed market value / cash breakdown."""

    institution = models.ForeignKey(
        "organizations.Organization", on_delete=models.PROTECT, related_name="investment_accounts"
    )
    branch = models.ForeignKey(
        "organizations.Branch",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="investment_accounts",
    )
    nickname = models.CharField(max_length=120)
    number = models.CharField(max_length=40, blank=True)  # displayed masked
    registration = models.CharField(
        max_length=20, choices=Registration.choices, default=Registration.TAXABLE_INDIVIDUAL
    )
    currency = models.ForeignKey("finance.Currency", on_delete=models.PROTECT, related_name="+")

    gl_account = models.OneToOneField(
        "finance.Account",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="investment_account",
    )

    is_active = models.BooleanField(default=True)

    opened_year = models.SmallIntegerField(null=True, blank=True)
    opened_month = models.SmallIntegerField(null=True, blank=True)
    opened_day = models.SmallIntegerField(null=True, blank=True)
    closed_year = models.SmallIntegerField(null=True, blank=True)
    closed_month = models.SmallIntegerField(null=True, blank=True)
    closed_day = models.SmallIntegerField(null=True, blank=True)

    notes = models.TextField(blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["nickname"]

    def __str__(self) -> str:
        return self.nickname

    @property
    def display(self) -> str:
        return self.nickname

    @property
    def masked_number(self) -> str:
        n = (self.number or "").strip()
        if not n:
            return ""
        return f"••••{n[-4:]}" if len(n) > 4 else n

    @property
    def registration_label(self) -> str:
        return self.get_registration_display()

    @property
    def group(self) -> str:
        return REGISTRATION_GROUP.get(self.registration, AccountGroup.TAXABLE)

    @property
    def group_label(self) -> str:
        return AccountGroup(self.group).label

    @property
    def group_tint(self) -> str:
        return GROUP_TINT.get(self.group, "slate")

    @property
    def tracks_contribution_year(self) -> bool:
        """IRA / HSA / 529 accounts attribute contributions to a specific tax year (prior-year
        contributions allowed until the filing deadline)."""
        return self.registration in CONTRIBUTION_YEAR_REGISTRATIONS

    # -- GL delegators (base currency; the account at cost) --

    @property
    def balance(self):
        """Base-currency GL balance (cost basis + cash), computed from posted lines."""
        if self.gl_account_id is None:
            return ZERO
        from apps.finance.services import account_balance

        return account_balance(self.gl_account)

    @property
    def native_balance(self):
        if self.gl_account_id is None:
            return None
        from apps.finance.services import account_native_balance

        return account_native_balance(self.gl_account)

    # -- Module-computed figures (account's own currency) --

    @property
    def cash_balance(self):
        from apps.investments.services import cash_balance

        return cash_balance(self)

    @property
    def cost_basis(self):
        from apps.investments.services import cost_basis

        return cost_basis(self)

    @property
    def market_value(self):
        from apps.investments.services import market_value

        return market_value(self)

    @property
    def unrealized_gain(self):
        return self.market_value - self.cost_basis

    @property
    def invested_value(self):
        """Market value of securities only (excludes settlement cash)."""
        return self.market_value

    @property
    def total_value(self):
        """Everything the account is worth today: settlement cash + securities at market."""
        return self.cash_balance + self.market_value

    @property
    def opened(self) -> PartialDate:
        return PartialDate.from_instance(self, "opened")

    @property
    def closed(self) -> PartialDate:
        return PartialDate.from_instance(self, "closed")

    @property
    def is_closed(self) -> bool:
        return self.closed.is_set


class InvestmentAccountHolder(TimeStampedModel):
    """A household member who holds an investment account (joint accounts have several). Unique per
    (account, person); one may be flagged primary."""

    account = models.ForeignKey(
        InvestmentAccount, on_delete=models.CASCADE, related_name="holders"
    )
    person = models.ForeignKey(
        "contacts.Person", on_delete=models.PROTECT, related_name="investment_holdings"
    )
    is_primary = models.BooleanField(default=False)

    class Meta:
        ordering = ["-is_primary", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["account", "person"], name="investmentaccountholder_unique"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.person} @ {self.account}"


# --- Transactions ----------------------------------------------------------------------------

class InvTxnType(models.TextChoices):
    OPENING = "opening", "Opening balance"          # cash (security null) or an opening holding
    CONTRIBUTION = "contribution", "Contribution"    # money in (external source / employer match)
    WITHDRAWAL = "withdrawal", "Withdrawal"          # money out (external)
    TRANSFER_IN = "transfer_in", "Transfer in"       # cash in from a tracked bank account
    TRANSFER_OUT = "transfer_out", "Transfer out"    # cash out to a tracked bank account
    BUY = "buy", "Buy"
    SELL = "sell", "Sell"
    DIVIDEND = "dividend", "Dividend"
    DIVIDEND_REINVEST = "dividend_reinvest", "Dividend (reinvested)"
    INTEREST = "interest", "Interest"
    CAP_GAIN_DIST = "cap_gain_dist", "Capital gains distribution"
    RETURN_OF_CAPITAL = "return_of_capital", "Return of capital"
    FEE = "fee", "Fee"
    SPLIT = "split", "Stock split"
    IN_KIND_IN = "in_kind_in", "Securities transfer in (in-kind)"
    IN_KIND_OUT = "in_kind_out", "Securities transfer out (in-kind)"
    WORTHLESS = "worthless", "Worthless write-off"      # bankruptcy: whole position → capital loss
    CASH_MERGER = "cash_merger", "Cash buyout / merger"  # going private: cash check for the shares
    MERGER = "merger", "Merger (stock-for-stock)"        # X → Y shares at a ratio; basis carries
    SPINOFF = "spinoff", "Spin-off"                      # allocate part of X's basis to a new Y
    # Leverage (IP5): a short is a negative-quantity lot with credit basis; margin is negative
    # settlement cash. No new liability accounts — the invariant absorbs both (see the docstring).
    SELL_SHORT = "sell_short", "Sell short"              # open a short: receive proceeds
    BUY_TO_COVER = "buy_to_cover", "Buy to cover"        # close a short: realized gain on cover
    MARGIN_INTEREST = "margin_interest", "Margin interest"          # interest paid to the broker
    DIV_PAID_SHORT = "div_paid_short", "Dividend paid (short)"      # payment-in-lieu to the lender
    # Options (IP5). `security` is the option contract (kind=OPTION); the underlying stock is on
    # `security.underlying`. A written (short) option is a negative-quantity lot, like a short.
    OPT_BUY_OPEN = "opt_buy_open", "Option — buy to open"      # long option: pay premium
    OPT_SELL_CLOSE = "opt_sell_close", "Option — sell to close"  # close a long option
    OPT_SELL_OPEN = "opt_sell_open", "Option — sell to open"   # write a short option: get premium
    OPT_BUY_CLOSE = "opt_buy_close", "Option — buy to close"   # close a written option
    OPT_EXPIRE = "opt_expire", "Option — expire"              # expires worthless (long loss / gain)
    OPT_EXERCISE = "opt_exercise", "Option — exercise"        # you exercise a long option
    OPT_ASSIGN = "opt_assign", "Option — assignment"          # your written option is assigned


# Types that require a security (the rest are cash-only / account-level).
SECURITY_TYPES = frozenset({
    InvTxnType.BUY, InvTxnType.SELL, InvTxnType.DIVIDEND_REINVEST,
    InvTxnType.RETURN_OF_CAPITAL, InvTxnType.SPLIT,
    InvTxnType.IN_KIND_IN, InvTxnType.IN_KIND_OUT,
    InvTxnType.WORTHLESS, InvTxnType.CASH_MERGER,
    InvTxnType.MERGER, InvTxnType.SPINOFF,
    InvTxnType.SELL_SHORT, InvTxnType.BUY_TO_COVER, InvTxnType.DIV_PAID_SHORT,
    InvTxnType.OPT_BUY_OPEN, InvTxnType.OPT_SELL_CLOSE, InvTxnType.OPT_SELL_OPEN,
    InvTxnType.OPT_BUY_CLOSE, InvTxnType.OPT_EXPIRE, InvTxnType.OPT_EXERCISE,
    InvTxnType.OPT_ASSIGN,
})

# Inflow types that count as a contribution on a tax-year-tracked account (IRA/HSA/529) and so carry
# a `tax_year`: money added from outside, whether an explicit contribution or a transfer from a
# tracked bank account (the usual IRA-funding path). Purely module metadata — never posted.
CONTRIBUTION_TAX_YEAR_TYPES = frozenset({
    InvTxnType.CONTRIBUTION, InvTxnType.TRANSFER_IN,
})

TXN_GLYPHS = {
    InvTxnType.OPENING: "pin",
    InvTxnType.CONTRIBUTION: "arrow-down",
    InvTxnType.WITHDRAWAL: "arrow-up",
    InvTxnType.TRANSFER_IN: "arrow-down",
    InvTxnType.TRANSFER_OUT: "arrow-up",
    InvTxnType.BUY: "arrow-up",
    InvTxnType.SELL: "arrow-down",
    InvTxnType.DIVIDEND: "coins",
    InvTxnType.DIVIDEND_REINVEST: "coins",
    InvTxnType.INTEREST: "coins",
    InvTxnType.CAP_GAIN_DIST: "coins",
    InvTxnType.RETURN_OF_CAPITAL: "arrow-down",
    InvTxnType.FEE: "arrow-up",
    InvTxnType.SPLIT: "network",
    InvTxnType.IN_KIND_IN: "download",
    InvTxnType.IN_KIND_OUT: "upload",
    InvTxnType.WORTHLESS: "trending-down",
    InvTxnType.CASH_MERGER: "banknote",
    InvTxnType.MERGER: "git-merge",
    InvTxnType.SPINOFF: "git-branch",
    InvTxnType.SELL_SHORT: "trending-down",
    InvTxnType.BUY_TO_COVER: "trending-up",
    InvTxnType.MARGIN_INTEREST: "percent",
    InvTxnType.DIV_PAID_SHORT: "coins",
    InvTxnType.OPT_BUY_OPEN: "download",
    InvTxnType.OPT_SELL_CLOSE: "upload",
    InvTxnType.OPT_SELL_OPEN: "pencil",
    InvTxnType.OPT_BUY_CLOSE: "circle-check",
    InvTxnType.OPT_EXPIRE: "clock",
    InvTxnType.OPT_EXERCISE: "shield-check",
    InvTxnType.OPT_ASSIGN: "inbox",
}


class CostBasisMethod(models.TextChoices):
    FIFO = "fifo", "First in, first out"
    SPECIFIC = "specific", "Specific lots"


class InvestmentTransaction(SoftDeleteModel):
    """One line in an investment account's register. Posts a balanced journal entry via the services
    module and drives the tax-lot engine (buys create lots, sells consume them). Posted entries are
    immutable, so an edit is a reverse-and-repost (bumping `posting_version`)."""

    account = models.ForeignKey(
        InvestmentAccount, on_delete=models.CASCADE, related_name="transactions"
    )
    txn_type = models.CharField(max_length=20, choices=InvTxnType.choices)
    date = models.DateField()

    security = models.ForeignKey(
        Security, on_delete=models.PROTECT, null=True, blank=True, related_name="transactions"
    )
    quantity = _qty(default=ZERO)   # shares/units (buy/sell/reinvest); pre-split count for SPLIT
    price = _price(default=ZERO)    # per-unit price (buy/sell/reinvest)
    amount = _amount(default=ZERO)  # gross cash: principal (buy), gross proceeds (sell), income…
    fee = _amount(default=ZERO)     # commission (capitalized) or advisory/account fee (expensed)

    # Stock split ratio, e.g. 2-for-1 → new=2, old=1; 1-for-10 reverse → new=1, old=10.
    # Reused by MERGER/SPINOFF as the share ratio (Y shares per old X share).
    split_ratio_new = models.DecimalField(
        max_digits=12, decimal_places=6, null=True, blank=True
    )
    split_ratio_old = models.DecimalField(
        max_digits=12, decimal_places=6, null=True, blank=True
    )

    # Corporate actions (MERGER/SPINOFF): the acquirer / spun-off security Y (`security` stays the
    # original X). Basis carries to Y for a merger; a spin-off moves `basis_pct`% of X's basis to Y.
    target_security = models.ForeignKey(
        Security, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    basis_pct = models.DecimalField(max_digits=7, decimal_places=4, null=True, blank=True)

    # Contra override: for CONTRIBUTION a revenue account marks it as income (e.g. employer match);
    # for FEE an alternate expense account. Null → the service's default contra for the type.
    category_account = models.ForeignKey(
        "finance.Account", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )

    # The tracked bank account on the other side of a cash transfer (nets via 1150 clearing).
    counter_account = models.ForeignKey(
        "banking.BankAccount",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="investment_transfers",
    )
    counter_external = models.CharField(max_length=160, blank=True)

    # In-kind securities transfer to/from the household's OTHER tracked investment account. Null →
    # external (gift/inheritance/RSU/ACATS from outside), which posts against opening equity instead
    # of the 1150 clearing account. On an OUT leg this is the destination; on the mirror IN leg, the
    # source.
    counter_investment_account = models.ForeignKey(
        InvestmentAccount,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="in_kind_transfers",
    )
    # The exact paired leg (OUT↔IN) of an internal in-kind transfer, so a re-sync targets the right
    # IN leg even when several transfers exist between the same two accounts.
    paired_txn = models.OneToOneField(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    # Tax lots moved by an in-kind transfer, as a snapshot preserving each lot's original
    # acquisition date + cost: [{"acquired_date": "YYYY-MM-DD", "quantity": "<dec>", "cost":
    # "<dec>"}]. On an OUT leg the engine materializes it (the lots actually consumed); on an
    # internal IN leg it is mirrored from the paired OUT leg; on an external IN leg it is user-set.
    lot_carry = models.JSONField(null=True, blank=True)

    payee_person = models.ForeignKey(
        "contacts.Person",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="investment_txns",
    )
    payee_organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="investment_txns",
    )

    # Sell lot selection: FIFO consumes oldest-first; SPECIFIC uses `lot_selection`
    # (a list of {"lot": <id>, "qty": "<decimal>"}), preserved so a repost reproduces the same draw.
    cost_basis_method = models.CharField(
        max_length=10, choices=CostBasisMethod.choices, default=CostBasisMethod.FIFO
    )
    lot_selection = models.JSONField(null=True, blank=True)
    realized_gain = _amount(default=ZERO)  # computed on SELL / excess return-of-capital

    # Contribution tax year (IRA/HSA/529 only): the tax year an incoming contribution / transfer-in
    # counts toward. Null for everything else. Module metadata — attribution only, never posted.
    tax_year = models.SmallIntegerField(null=True, blank=True)

    memo = models.CharField(max_length=255, blank=True)
    reference = models.CharField(max_length=60, blank=True)
    cleared = models.BooleanField(default=False)  # reconciliation-lite; never affects the GL

    journal_entry = models.ForeignKey(
        "finance.JournalEntry", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    posting_version = models.PositiveIntegerField(default=1)

    history = HistoricalRecords()

    class Meta:
        ordering = ["-date", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(
                    payee_person__isnull=False, payee_organization__isnull=False
                ),
                name="investmenttransaction_one_payee",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gte=0), name="investmenttransaction_amount_nonneg"
            ),
            models.CheckConstraint(
                condition=~models.Q(counter_investment_account=models.F("account")),
                name="investmenttransaction_no_self_inkind",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_txn_type_display()} {self.amount} on {self.date}"

    @property
    def type_label(self) -> str:
        return self.get_txn_type_display()

    @property
    def type_glyph(self) -> str:
        return TXN_GLYPHS.get(self.txn_type, "circle")

    @property
    def net_proceeds(self):
        """Sell proceeds net of commission (commission reduces proceeds / is capitalized)."""
        return self.amount - self.fee

    @property
    def signed_cash(self):
        """Effect of this transaction on the account's settlement cash balance."""
        t = self.txn_type
        if t == InvTxnType.OPENING:
            return self.amount if self.security_id is None else ZERO
        if t in (InvTxnType.CONTRIBUTION, InvTxnType.TRANSFER_IN, InvTxnType.DIVIDEND,
                 InvTxnType.INTEREST, InvTxnType.CAP_GAIN_DIST, InvTxnType.RETURN_OF_CAPITAL,
                 InvTxnType.CASH_MERGER):
            return self.amount  # CASH_MERGER: the buyout check comes in as cash
        if t in (InvTxnType.WITHDRAWAL, InvTxnType.TRANSFER_OUT, InvTxnType.FEE,
                 InvTxnType.MARGIN_INTEREST, InvTxnType.DIV_PAID_SHORT):
            return -self.amount
        if t in (InvTxnType.BUY, InvTxnType.BUY_TO_COVER,
                 InvTxnType.OPT_BUY_OPEN, InvTxnType.OPT_BUY_CLOSE):
            return -(self.amount + self.fee)  # cash paid to buy / cover / open-long / close-written
        if t in (InvTxnType.SELL, InvTxnType.SELL_SHORT,
                 InvTxnType.OPT_SELL_OPEN, InvTxnType.OPT_SELL_CLOSE):
            return self.net_proceeds  # short-sale / option-write proceeds come in as cash
        if t in (InvTxnType.OPT_EXERCISE, InvTxnType.OPT_ASSIGN):
            # Cash = strike × shares (self.amount). Exercising a long PUT or being assigned on a
            # written CALL SELLS the underlying (cash in); the mirror cases BUY it (cash out).
            right = self.security.option_right if self.security_id else ""
            cash_in = (
                (t == InvTxnType.OPT_EXERCISE and right == OptionRight.PUT)
                or (t == InvTxnType.OPT_ASSIGN and right == OptionRight.CALL)
            )
            return self.net_proceeds if cash_in else -(self.amount + self.fee)
        # DIVIDEND_REINVEST, SPLIT, MERGER, SPINOFF, in-kind, WORTHLESS and OPT_EXPIRE are
        # cash-neutral.
        return ZERO

    @property
    def is_inflow(self) -> bool:
        return self.signed_cash > 0

    @property
    def direction(self) -> str:
        sc = self.signed_cash
        return "in" if sc > 0 else ("out" if sc < 0 else "flat")

    @property
    def payee(self):
        return self.payee_person or self.payee_organization

    @property
    def split_ratio_display(self) -> str:
        if self.split_ratio_new and self.split_ratio_old:
            new = self.split_ratio_new.normalize()
            old = self.split_ratio_old.normalize()
            return f"{new}-for-{old}"
        return ""

    @property
    def option_contracts(self):
        """Contract count for an option transaction (`quantity` is stored shares-equivalent =
        contracts × the contract multiplier); the raw quantity otherwise."""
        if self.security_id and self.security.is_option and self.security.multiplier:
            return self.quantity / self.security.multiplier
        return self.quantity

    @property
    def is_managed_in_leg(self) -> bool:
        """True for the auto-created IN leg of an *internal* in-kind transfer — it is a managed
        mirror of its paired OUT leg, so the UI blocks editing/deleting it directly (external
        in-kind INs, which the user does enter by hand, have no counter account)."""
        return (
            self.txn_type == InvTxnType.IN_KIND_IN
            and self.counter_investment_account_id is not None
        )


class Lot(TimeStampedModel):
    """An open tax lot: a quantity of a security acquired on a date at a cost. Sells consume lots
    (see `LotConsumption`); `remaining_quantity`/`cost_basis` track what's left. Engine state is
    managed exclusively by the services module — never edited directly."""

    account = models.ForeignKey(InvestmentAccount, on_delete=models.CASCADE, related_name="lots")
    security = models.ForeignKey(Security, on_delete=models.PROTECT, related_name="lots")
    acquired_date = models.DateField()

    original_quantity = _qty()
    remaining_quantity = _qty()
    original_cost = _amount()   # cost basis of original_quantity (commission capitalized)
    cost_basis = _amount()      # cost basis of remaining_quantity

    open = models.BooleanField(default=True)
    source_txn = models.ForeignKey(
        InvestmentTransaction,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="lots_created",
    )

    class Meta:
        ordering = ["acquired_date", "id"]

    def __str__(self) -> str:
        return f"{self.remaining_quantity} {self.security} @ {self.acquired_date}"

    @property
    def per_share_cost(self):
        if self.remaining_quantity and self.remaining_quantity != ZERO:
            return self.cost_basis / self.remaining_quantity
        if self.original_quantity and self.original_quantity != ZERO:
            return self.original_cost / self.original_quantity
        return ZERO

    @property
    def market_value(self):
        price = self.security.latest_price
        if price is None:
            return self.cost_basis
        return (self.remaining_quantity * price).quantize(Decimal("0.0001"))

    @property
    def unrealized_gain(self):
        return self.market_value - self.cost_basis


class LotConsumption(TimeStampedModel):
    """Audit of a SELL (or excess return-of-capital) drawing cost basis from a specific lot — makes
    sells reversible: reversing a sale restores each lot's `remaining_quantity`/`cost_basis`."""

    sale_txn = models.ForeignKey(
        InvestmentTransaction, on_delete=models.CASCADE, related_name="lot_consumptions"
    )
    lot = models.ForeignKey(Lot, on_delete=models.PROTECT, related_name="consumptions")
    quantity = _qty()
    cost = _amount()       # cost basis drawn from the lot
    proceeds = _amount(default=ZERO)  # proceeds allocated to this draw (for per-lot realized gain)

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.quantity} from lot #{self.lot_id}"

    @property
    def realized_gain(self):
        return self.proceeds - self.cost


# --- Vesting (employer match & equity grants) ------------------------------------------------

class VestingKind(models.TextChoices):
    DOLLAR = "dollar", "Employer match ($)"
    SHARES = "shares", "Equity grant (shares)"


_FRAC = Decimal("0.000001")
_HUNDRED = Decimal("100")


class VestingGrant(SoftDeleteModel):
    """A vesting overlay: a total (dollars for a 401(k)-style employer match, or shares for an RSU /
    equity grant) that vests on a custom `VestingTranche` schedule. Purely a MODULE-level view — it
    posts NOTHING to the GL and never touches tax lots (consistent with "cost in the GL, value in
    the module"). `funded` marks whether the total already sits inside the account balance: funded
    (typical match) → its unvested part is AT-RISK and reduces the module's vested value; unfunded
    (typical RSU) → its unvested part is UPCOMING/future — shown but not subtracted."""

    account = models.ForeignKey(
        InvestmentAccount, on_delete=models.CASCADE, related_name="vesting_grants"
    )
    kind = models.CharField(max_length=8, choices=VestingKind.choices, default=VestingKind.DOLLAR)
    security = models.ForeignKey(
        Security, on_delete=models.PROTECT, null=True, blank=True, related_name="vesting_grants"
    )
    label = models.CharField(max_length=120)
    grant_date = models.DateField()
    total = _qty(default=ZERO)   # dollars (kind=dollar) or shares (kind=shares)
    funded = models.BooleanField(default=True)  # already reflected in the account's balance?
    notes = models.CharField(max_length=255, blank=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["grant_date", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(total__gte=0), name="vestinggrant_total_nonneg"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.label} ({self.get_kind_display()})"

    @property
    def is_shares(self) -> bool:
        return self.kind == VestingKind.SHARES

    @property
    def glyph(self) -> str:
        return "trending-up" if self.is_shares else "banknote"

    @property
    def tint(self) -> str:
        if self.is_shares and self.security_id:
            return self.security.tint
        return "investments"

    def vested_fraction(self, as_of=None) -> Decimal:
        """Highest cumulative tranche % with vest_date <= as_of (else 0), as a 0..1 fraction."""
        as_of = as_of or datetime.date.today()
        pct = ZERO
        for tr in self.tranches.all():
            if tr.vest_date <= as_of:
                pct = max(pct, tr.cumulative_percent)
        return (pct / _HUNDRED).quantize(_FRAC)

    def vested(self, as_of=None) -> Decimal:
        return (self.total * self.vested_fraction(as_of)).quantize(_FRAC)

    def unvested(self, as_of=None) -> Decimal:
        return (self.total - self.vested(as_of)).quantize(_FRAC)

    def _unit_value(self, qty: Decimal) -> Decimal:
        """Dollar value of `qty` units: dollars are already $; shares × latest price (0 if none)."""
        if not self.is_shares:
            return qty.quantize(Decimal("0.0001"))
        price = self.security.latest_price if self.security_id else None
        if price is None:
            return ZERO
        return (qty * price).quantize(Decimal("0.0001"))

    def vested_value(self, as_of=None) -> Decimal:
        return self._unit_value(self.vested(as_of))

    def unvested_value(self, as_of=None) -> Decimal:
        return self._unit_value(self.unvested(as_of))

    def next_vest(self, as_of=None):
        """The next tranche strictly after `as_of` (the upcoming vest event), or None."""
        as_of = as_of or datetime.date.today()
        for tr in self.tranches.all():  # ordered by vest_date
            if tr.vest_date > as_of:
                return tr
        return None


class VestingTranche(TimeStampedModel):
    """One step of a grant's custom vesting schedule: by `vest_date`, cumulatively
    `cumulative_percent` of the grant is vested. Replace-all managed from the grant form."""

    grant = models.ForeignKey(VestingGrant, on_delete=models.CASCADE, related_name="tranches")
    vest_date = models.DateField()
    cumulative_percent = models.DecimalField(max_digits=5, decimal_places=2)  # 0..100

    class Meta:
        ordering = ["vest_date", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["grant", "vest_date"], name="vestingtranche_unique_date"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.cumulative_percent}% @ {self.vest_date}"
