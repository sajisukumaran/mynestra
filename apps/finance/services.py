"""The finance service layer — the ONLY sanctioned way to write to the general ledger.

Future modules (Banking, bills, …) call `post_entry(...)` with a list of `LineInput`s; they never
create `JournalEntry`/`JournalLine` rows directly. The service enforces double-entry integrity
(Σ base debits == Σ base credits), converts each line to the household base currency, assigns a
per-fiscal-year sequential number, links an optional source document idempotently, and resolves the
fiscal period (auto-creating the Jan–Dec calendar on first use). Posted entries are immutable —
`reverse_entry()` creates a mirror entry; they are never edited or deleted.

Balance queries (added alongside) compute account balances from posted lines on demand — the single
source of truth (no materialized balance table).
"""

from __future__ import annotations

import calendar
import datetime
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from django.db import IntegrityError, connection, transaction
from django.db.models import Sum
from django.utils import timezone

from apps.finance.exceptions import (
    ClosedPeriod,
    COAEditError,
    EmptyEntry,
    InvalidLine,
    MissingExchangeRate,
    PostedEntryImmutable,
    UnbalancedEntry,
    UnknownAccount,
)
from apps.finance.models import (
    DEBIT_NORMAL_TYPES,
    ZERO,
    Account,
    AccountType,
    Currency,
    ExchangeRate,
    FiscalYear,
    JournalEntry,
    JournalLine,
    PostingMap,
    default_side_for,
)

ONE = Decimal("1")


# --- Currency / FX resolvers ----------------------------------------------------------------

def base_currency() -> Currency:
    """The household functional/base currency (the ledger is kept in it). Reads `Tenant.currency`
    when available; falls back to USD so posting never crashes on an unconfigured tenant."""
    code = getattr(getattr(connection, "tenant", None), "currency", "") or ""
    if not code:
        code = _tenant_currency_from_table() or "USD"
    currency, _ = Currency.objects.get_or_create(
        code=code, defaults={"name": code, "symbol": code, "is_system": True}
    )
    return currency


def _tenant_currency_from_table() -> str | None:
    """Fallback for schema_context paths (connection.tenant is a FakeTenant without `.currency`)."""
    try:
        from apps.tenants.models import Tenant

        return (
            Tenant.objects.filter(schema_name=connection.schema_name)
            .values_list("currency", flat=True)
            .first()
        )
    except Exception:  # noqa: BLE001 — field may not exist yet (pre-localization) / no row
        return None


def _resolve_currency(ref) -> Currency | None:
    if ref is None:
        return None
    if isinstance(ref, Currency):
        return ref
    return Currency.objects.get(code=ref)


def rate_to_base(currency, on_date: datetime.date, *, explicit=None) -> Decimal:
    """Units of base per 1 unit of `currency` on/before `on_date`. base→base is 1; an explicit rate
    wins; otherwise the latest `ExchangeRate` is used, or `MissingExchangeRate` is raised."""
    base = base_currency()
    code = currency.code if isinstance(currency, Currency) else str(currency)
    if code == base.code:
        return ONE
    if explicit is not None:
        return Decimal(explicit)
    rate = (
        ExchangeRate.objects.filter(currency_id=code, as_of__lte=on_date)
        .values_list("rate", flat=True)
        .first()
    )
    if rate is None:
        raise MissingExchangeRate(f"No exchange rate for {code} on or before {on_date}.")
    return rate


def _round_base(amount: Decimal, base: Currency) -> Decimal:
    quantum = Decimal(1).scaleb(-base.decimal_places)  # 0.01 for 2dp, 1 for 0dp (JPY)
    return amount.quantize(quantum, rounding=ROUND_HALF_UP)


# --- Fiscal calendar ------------------------------------------------------------------------

def _ensure_periods(fiscal_year: FiscalYear) -> None:
    from apps.finance.models import FiscalPeriod

    for month in range(1, 13):
        last_day = calendar.monthrange(fiscal_year.year, month)[1]
        FiscalPeriod.objects.get_or_create(
            fiscal_year=fiscal_year,
            period_no=month,
            defaults={
                "name": f"{calendar.month_abbr[month]} {fiscal_year.year}",
                "start_date": datetime.date(fiscal_year.year, month, 1),
                "end_date": datetime.date(fiscal_year.year, month, last_day),
            },
        )


def resolve_period(d: datetime.date, *, create: bool = True):
    """The FiscalPeriod for date `d` (Jan–Dec). Auto-creates the year + 12 months on first use."""
    from apps.finance.models import FiscalPeriod

    fiscal_year = FiscalYear.objects.filter(year=d.year).first()
    if fiscal_year is None:
        if not create:
            return None
        fiscal_year, _ = FiscalYear.objects.get_or_create(
            year=d.year,
            defaults={
                "start_date": datetime.date(d.year, 1, 1),
                "end_date": datetime.date(d.year, 12, 31),
            },
        )
    period = FiscalPeriod.objects.filter(fiscal_year=fiscal_year, period_no=d.month).first()
    if period is None and create:
        _ensure_periods(fiscal_year)
        period = FiscalPeriod.objects.get(fiscal_year=fiscal_year, period_no=d.month)
    return period


def _next_entry_no(fiscal_year: FiscalYear) -> int:
    """Next per-year sequential number (monotonic, gap-tolerant). Call inside a transaction."""
    locked = FiscalYear.objects.select_for_update().get(pk=fiscal_year.pk)
    locked.last_entry_no += 1
    locked.save(update_fields=["last_entry_no", "updated_at"])
    return locked.last_entry_no


# --- Account resolution ---------------------------------------------------------------------

def resolve_account(ref) -> Account:
    """Resolve an Account instance, a COA `code`, or a `system_key` — else UnknownAccount."""
    if isinstance(ref, Account):
        return ref
    account = (
        Account.objects.filter(system_key=str(ref)).first()
        or Account.objects.filter(code=str(ref)).first()
    )
    if account is None:
        raise UnknownAccount(f"No account matches {ref!r}.")
    return account


# --- Accounting mode + per-owner posting maps (Standard/Expert seam) -------------------------

def _tenant_accounting_mode() -> str:
    """The active tenant's accounting mode ('standard'/'expert'), with a schema_context fallback
    (connection.tenant is a FakeTenant without `.accounting_mode` under schema_context)."""
    mode = getattr(getattr(connection, "tenant", None), "accounting_mode", "") or ""
    if mode:
        return mode
    try:
        from apps.tenants.models import Tenant

        return (
            Tenant.objects.filter(schema_name=connection.schema_name)
            .values_list("accounting_mode", flat=True)
            .first()
        ) or "standard"
    except Exception:  # noqa: BLE001 — field may not exist yet / no row
        return "standard"


def is_expert_mode() -> bool:
    return _tenant_accounting_mode() == "expert"


def _posting_map_content_type(owner):
    from django.contrib.contenttypes.models import ContentType

    return ContentType.objects.get_for_model(owner.__class__)


def resolve_posting_account(owner, activity: str, default_ref) -> Account:
    """The account a subledger `activity` posts to.

    Standard mode (or no owner): the subledger's built-in `default_ref`. Expert mode: a per-owner
    `PostingMap` override for (owner, activity) if one exists, else `default_ref`. Raises
    UnknownAccount if a mapped account has since been removed — Expert users own COA deletions, and
    callers surface this as a "fix your accounting setup" message rather than crashing."""
    if owner is not None and is_expert_mode():
        pm = (
            PostingMap.objects.filter(
                content_type=_posting_map_content_type(owner),
                object_id=owner.pk,
                activity=activity,
            )
            .values_list("account_id", flat=True)
            .first()
        )
        if pm is not None:
            account = Account.objects.filter(pk=pm).first()  # excludes soft-deleted
            if account is None:
                raise UnknownAccount(
                    f"The account mapped for '{activity}' no longer exists — "
                    "update the account's Accounting setup."
                )
            return account
    return resolve_account(default_ref)


def set_posting_map(owner, activity: str, account) -> None:
    """Upsert a per-owner activity→account override; `account=None` clears it. Used by the
    Accounting Setup tab. (Only consulted in Expert mode, but safe to write in any mode.)"""
    ct = _posting_map_content_type(owner)
    if account in (None, "", 0, "0"):
        PostingMap.objects.filter(content_type=ct, object_id=owner.pk, activity=activity).delete()
        return
    account = account if isinstance(account, Account) else resolve_account(account)
    PostingMap.objects.update_or_create(
        content_type=ct, object_id=owner.pk, activity=activity, defaults={"account": account}
    )


def posting_map_for(owner) -> dict[str, int]:
    """{activity: account_id} for an owner — prefills the Accounting Setup tab."""
    return dict(
        PostingMap.objects.filter(
            content_type=_posting_map_content_type(owner), object_id=owner.pk
        ).values_list("activity", "account_id")
    )


# --- Chart-of-Accounts editing (Expert mode only) -------------------------------------------
# Mutations here can break Standard-mode automatic posting, so any Standard-critical change to a
# seeded (`is_system`) account sets the sticky `accounting_locked` flag — after which the tenant
# can no longer switch back to Standard. Rename/description edits are not critical.

def account_has_postings(account) -> bool:
    return JournalLine.objects.filter(account=account).exists()


def account_has_children(account) -> bool:
    return Account.objects.filter(parent=account).exists()


def lock_accounting_mode() -> None:
    """Sticky-lock: this tenant can no longer switch back to Standard. Idempotent."""
    from apps.tenants.models import Tenant

    Tenant.objects.filter(
        schema_name=connection.schema_name, accounting_locked=False
    ).update(accounting_locked=True)


def _assert_code_free(code: str, *, exclude_pk=None) -> None:
    qs = Account.objects.filter(code=code)
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    if qs.exists():
        raise COAEditError(f"Account code {code!r} is already in use.")


def _would_cycle(account, parent) -> bool:
    """True if `account.parent = parent` would create a cycle (parent is self or a descendant)."""
    node = parent
    while node is not None:
        if node.pk == account.pk:
            return True
        node = node.parent
    return False


def create_account(*, code, name, account_type, parent=None, is_postable=True,
                   description="", currency=None) -> Account:
    code = (code or "").strip()
    name = (name or "").strip()
    if not code or not name:
        raise COAEditError("Code and name are both required.")
    if account_type not in AccountType.values:
        raise COAEditError("Choose a valid account type.")
    _assert_code_free(code)
    return Account.objects.create(
        code=code, name=name, description=description or "",
        type=account_type, normal_side=default_side_for(account_type),
        parent=parent, currency=currency,
        is_postable=is_postable, is_active=True, is_system=False,
    )


def edit_account(account, *, code, name, account_type, parent=None, is_postable=True,
                 is_active=True, description="") -> Account:
    code = (code or "").strip()
    name = (name or "").strip()
    if not code or not name:
        raise COAEditError("Code and name are both required.")
    if account_type not in AccountType.values:
        raise COAEditError("Choose a valid account type.")
    _assert_code_free(code, exclude_pk=account.pk)
    if parent is not None and _would_cycle(account, parent):
        raise COAEditError("An account can't be a child of itself.")
    if not is_postable and account_has_postings(account):
        raise COAEditError("This account has posted entries, so it can't become a header.")
    if is_postable and account_has_children(account):
        raise COAEditError("This account has sub-accounts, so it must stay a header.")

    new_parent_id = parent.pk if parent else None
    critical = account.is_system and (
        code != account.code
        or account_type != account.type
        or new_parent_id != account.parent_id
        or is_postable != account.is_postable
        or (account.is_active and not is_active)
    )

    account.code = code
    account.name = name
    account.description = description or ""
    if account_type != account.type:  # preserve a deliberate contra normal_side otherwise
        account.type = account_type
        account.normal_side = default_side_for(account_type)
    account.parent = parent
    account.is_postable = is_postable
    account.is_active = is_active
    account.save()
    if critical:
        lock_accounting_mode()
    return account


def delete_account(account) -> None:
    if account_has_postings(account):
        raise COAEditError("This account has posted entries and can't be deleted.")
    if account_has_children(account):
        raise COAEditError("This account has sub-accounts — remove or reparent them first.")
    was_system = account.is_system
    account.delete()  # soft-delete (frees the code; Recently-deleted can restore)
    if was_system:
        lock_accounting_mode()


# --- Posting --------------------------------------------------------------------------------

@dataclass(frozen=True)
class LineInput:
    """One posting for `post_entry`. `account` is an Account, a COA code, or a system_key.
    `currency`/`fx_rate` default to the base currency at rate 1 (resolved from ExchangeRate when a
    foreign currency is given without an explicit rate). `person`/`organization` are an optional
    counterparty (at most one)."""

    account: object
    debit: Decimal = ZERO
    credit: Decimal = ZERO
    currency: object | None = None
    fx_rate: Decimal | None = None
    memo: str = ""
    person: object | None = None
    organization: object | None = None


def _existing_by_key(external_key: str):
    if not external_key:
        return None
    return (
        JournalEntry.objects.filter(external_key=external_key)
        .exclude(status=JournalEntry.Status.VOID)
        .first()
    )


def _prepare_lines(lines: Sequence[LineInput], date: datetime.date, base: Currency) -> list:
    """Validate + FX-convert each line and assert base-currency balance. Returns prepared tuples
    (account, currency, debit, credit, rate, base_debit, base_credit, li) for row creation. Shared
    by `post_entry` and `repost_entry` so both apply identical integrity rules."""
    total_debit = ZERO
    total_credit = ZERO
    prepared = []
    for li in lines:
        account = resolve_account(li.account)
        if not account.is_postable:
            raise InvalidLine(f"Account {account.code} is a header account; cannot post to it.")
        if not account.is_active:
            raise InvalidLine(f"Account {account.code} is inactive.")

        line_currency = _resolve_currency(li.currency) or base
        if not line_currency.is_active:
            raise InvalidLine(f"Currency {line_currency.code} is inactive.")

        debit = Decimal(li.debit or 0)
        credit = Decimal(li.credit or 0)
        if debit < 0 or credit < 0:
            raise InvalidLine("Line amounts must be non-negative.")
        if (debit > 0) == (credit > 0):
            raise InvalidLine("A line must have exactly one of debit/credit greater than zero.")
        if li.person is not None and li.organization is not None:
            raise InvalidLine("A line may reference at most one counterparty (person or org).")

        rate = Decimal(li.fx_rate) if li.fx_rate is not None else rate_to_base(line_currency, date)
        base_debit = _round_base(debit * rate, base)
        base_credit = _round_base(credit * rate, base)
        total_debit += base_debit
        total_credit += base_credit
        prepared.append((account, line_currency, debit, credit, rate, base_debit, base_credit, li))

    if total_debit != total_credit:
        raise UnbalancedEntry(
            f"Entry unbalanced in {base.code}: debits {total_debit} != credits {total_credit}."
        )
    return prepared


def post_entry(
    *,
    date: datetime.date,
    lines: Sequence[LineInput],
    description: str = "",
    memo: str = "",
    reference: str = "",
    entry_type: str = JournalEntry.EntryType.STANDARD,
    currency=None,
    source=None,
    external_key: str = "",
    status: str = JournalEntry.Status.POSTED,
    user=None,
) -> JournalEntry:
    """Create a balanced entry. Raises on imbalance/bad lines. Idempotent on external_key."""
    prior = _existing_by_key(external_key)
    if prior is not None:
        return prior

    if len(lines) < 2:
        raise EmptyEntry("A journal entry needs at least two lines.")

    base = base_currency()
    entry_currency = _resolve_currency(currency) or base
    prepared = _prepare_lines(lines, date, base)

    with transaction.atomic():
        period = resolve_period(date)
        entry = JournalEntry(
            date=date,
            period=period,
            entry_type=entry_type,
            description=description,
            memo=memo,
            reference=reference,
            status=status,
            currency=entry_currency,
            external_key=external_key or "",
        )
        if source is not None:
            entry.source = source
        if status == JournalEntry.Status.POSTED:
            entry.posted_at = timezone.now()
            entry.posted_by = user
            entry.fiscal_year = period.fiscal_year.year
            entry.entry_no = _next_entry_no(period.fiscal_year)
        try:
            with transaction.atomic():
                entry.save()
        except IntegrityError:
            # Race on external_key: another poster inserted the same source. Reuse theirs.
            prior = _existing_by_key(external_key)
            if prior is not None:
                return prior
            raise

        for account, line_currency, debit, credit, rate, base_debit, base_credit, li in prepared:
            JournalLine.objects.create(
                entry=entry,
                account=account,
                currency=line_currency,
                debit=debit,
                credit=credit,
                fx_rate=rate,
                base_debit=base_debit,
                base_credit=base_credit,
                memo=li.memo,
                person=li.person,
                organization=li.organization,
            )
    return entry


def reverse_entry(entry: JournalEntry, *, date=None, memo: str = "", user=None) -> JournalEntry:
    """Post a mirror-image entry that nets `entry` to zero. The original is kept, not deleted."""
    if entry.status != JournalEntry.Status.POSTED:
        raise PostedEntryImmutable("Only a posted entry can be reversed.")
    reversal_date = date or entry.date
    with transaction.atomic():
        period = resolve_period(reversal_date)
        rev = JournalEntry(
            date=reversal_date,
            period=period,
            entry_type=JournalEntry.EntryType.REVERSAL,
            description=f"Reversal of JE#{entry.entry_no or entry.pk}",
            memo=memo,
            status=JournalEntry.Status.POSTED,
            currency=entry.currency,
            reversal_of=entry,
            external_key=f"{entry.external_key}:rev" if entry.external_key else "",
            posted_at=timezone.now(),
            posted_by=user,
            fiscal_year=period.fiscal_year.year,
            entry_no=_next_entry_no(period.fiscal_year),
        )
        rev.save()
        for line in entry.lines.all():
            JournalLine.objects.create(
                entry=rev,
                account=line.account,
                currency=line.currency,
                debit=line.credit,
                credit=line.debit,
                fx_rate=line.fx_rate,
                base_debit=line.base_credit,
                base_credit=line.base_debit,
                memo=line.memo,
                person=line.person,
                organization=line.organization,
            )
        JournalEntry.objects.filter(pk=entry.pk).update(is_reversed=True)
        entry.is_reversed = True
    return rev


def void_entry(entry: JournalEntry, *, user=None) -> JournalEntry:
    """Void a DRAFT entry (never affects balances). A POSTED entry must be reversed instead."""
    if entry.status == JournalEntry.Status.POSTED:
        raise PostedEntryImmutable("A posted entry must be reversed, not voided.")
    entry.status = JournalEntry.Status.VOID
    entry.save(update_fields=["status", "updated_at"])
    return entry


def repost_entry(
    entry: JournalEntry,
    *,
    lines: Sequence[LineInput],
    date=None,
    description: str | None = None,
    memo: str | None = None,
    user=None,
) -> JournalEntry:
    """**In-place edit** of a posted entry: rewrite its lines (and optionally date/description) to
    the new values with NO reversal — the same `JournalEntry` row, its `entry_no`, `external_key`
    and `source` preserved, its lines replaced.

    This deliberately departs from the ledger's immutable reverse-and-repost rule and is scoped to
    editable subledger *documents* (Payables bills) whose source module owns them; Banking / Cards /
    Investments keep `reverse_entry`. Refused for reversal entries and for entries whose new period
    is closed, so the derived-close guarantee still holds for closed periods. Trade-off: no GL-level
    history of the prior lines (the source document's own simple-history records the change)."""
    if entry.status != JournalEntry.Status.POSTED:
        raise PostedEntryImmutable("Only a posted entry can be reposted in place.")
    if entry.entry_type == JournalEntry.EntryType.REVERSAL:
        raise PostedEntryImmutable("A reversal entry cannot be reposted.")
    if len(lines) < 2:
        raise EmptyEntry("A journal entry needs at least two lines.")

    base = base_currency()
    new_date = date or entry.date
    prepared = _prepare_lines(lines, new_date, base)

    with transaction.atomic():
        period = resolve_period(new_date)
        if period is None or period.is_closed or period.fiscal_year.is_closed:
            raise ClosedPeriod("The target period is closed; this entry can't be edited.")

        entry.date = new_date
        entry.period = period
        if description is not None:
            entry.description = description
        if memo is not None:
            entry.memo = memo
        # entry_no is per fiscal year; if the date moved to a different year, take a fresh number.
        if entry.fiscal_year != period.fiscal_year.year:
            entry.fiscal_year = period.fiscal_year.year
            entry.entry_no = _next_entry_no(period.fiscal_year)
        entry.save()

        entry.lines.all().delete()  # JournalLine is append-only, not soft-deleted: a real delete
        for account, line_currency, debit, credit, rate, base_debit, base_credit, li in prepared:
            JournalLine.objects.create(
                entry=entry,
                account=account,
                currency=line_currency,
                debit=debit,
                credit=credit,
                fx_rate=rate,
                base_debit=base_debit,
                base_credit=base_credit,
                memo=li.memo,
                person=li.person,
                organization=li.organization,
            )
    return entry


# --- Balances (computed from posted lines — the single source of truth) ---------------------

@dataclass(frozen=True)
class TrialBalanceRow:
    account: Account
    debit_total: Decimal
    credit_total: Decimal
    balance: Decimal  # natural balance (signed by the account's normal side)


def _posted_lines(as_of=None):
    qs = JournalLine.objects.filter(entry__status=JournalEntry.Status.POSTED)
    if as_of is not None:
        qs = qs.filter(entry__date__lte=as_of)
    return qs


def _raw(qs) -> Decimal:
    """Σ base_debit − Σ base_credit over a line queryset."""
    agg = qs.aggregate(d=Sum("base_debit"), c=Sum("base_credit"))
    return (agg["d"] or ZERO) - (agg["c"] or ZERO)


def _descendant_ids(account: Account) -> list[int]:
    """Account subtree (self + all descendants) — small tree, walked in Python."""
    ids = [account.pk]
    stack = [account.pk]
    while stack:
        parent_id = stack.pop()
        for child_id in Account.objects.filter(parent_id=parent_id).values_list("pk", flat=True):
            ids.append(child_id)
            stack.append(child_id)
    return ids


def account_raw_balance(account, *, as_of=None) -> Decimal:
    """Signed (debit − credit) base balance of the account's subtree (rolls up header accounts)."""
    account = account if isinstance(account, Account) else resolve_account(account)
    return _raw(_posted_lines(as_of).filter(account_id__in=_descendant_ids(account)))


def account_balance(account, *, as_of=None) -> Decimal:
    """Natural balance (positive in the account's normal direction), with header rollups."""
    account = account if isinstance(account, Account) else resolve_account(account)
    return account_raw_balance(account, as_of=as_of) * account.normal_sign


def account_native_balance(account, *, as_of=None) -> Decimal | None:
    """A currency-tagged account's balance in its OWN currency (from txn amounts); else None."""
    account = account if isinstance(account, Account) else resolve_account(account)
    if account.currency_id is None:
        return None
    qs = _posted_lines(as_of).filter(account=account)
    agg = qs.aggregate(d=Sum("debit"), c=Sum("credit"))
    raw = (agg["d"] or ZERO) - (agg["c"] or ZERO)
    return raw * account.normal_sign


def trial_balance(*, as_of=None) -> list[TrialBalanceRow]:
    """One row per account with posted activity; Σ debit_total == Σ credit_total (base)."""
    grouped = (
        _posted_lines(as_of)
        .values("account")
        .annotate(d=Sum("base_debit"), c=Sum("base_credit"))
    )
    totals = {row["account"]: (row["d"] or ZERO, row["c"] or ZERO) for row in grouped}
    rows = []
    for account in Account.objects.filter(pk__in=totals).order_by("code"):
        debit_total, credit_total = totals[account.pk]
        balance = (debit_total - credit_total) * account.normal_sign
        rows.append(TrialBalanceRow(account, debit_total, credit_total, balance))
    return rows


# --- Derived close: equity/net worth computed by date, never physically closed --------------

def _type_total(account_type: str, *, start=None, end=None, as_of=None) -> Decimal:
    """Natural (positive) base total for all accounts of a type over a date window."""
    qs = _posted_lines(as_of if end is None else None).filter(account__type=account_type)
    if start is not None:
        qs = qs.filter(entry__date__gte=start)
    if end is not None:
        qs = qs.filter(entry__date__lte=end)
    sign = 1 if account_type in DEBIT_NORMAL_TYPES else -1
    return _raw(qs) * sign


def net_income(*, start=None, end=None) -> Decimal:
    """Revenue − expense (base) over the date window."""
    revenue = _type_total(AccountType.REVENUE, start=start, end=end)
    expense = _type_total(AccountType.EXPENSE, start=start, end=end)
    return revenue - expense


def current_year_earnings(*, as_of=None) -> Decimal:
    """Net income for the as-of fiscal year, to date."""
    as_of = as_of or datetime.date.today()
    return net_income(start=datetime.date(as_of.year, 1, 1), end=as_of)


def retained_earnings(*, as_of=None) -> Decimal:
    """Cumulative net income of all prior fiscal years (base)."""
    as_of = as_of or datetime.date.today()
    return net_income(end=datetime.date(as_of.year - 1, 12, 31))


def _contingent_liability_total(*, as_of=None) -> Decimal:
    """Natural (positive) base amount owed on the contingent-liabilities subtree (2950); ZERO when
    the account isn't present (tenants predating the Loans module)."""
    account = Account.objects.filter(system_key="contingent_liabilities").first()
    if account is None:
        return ZERO
    return account_balance(account, as_of=as_of)


def net_worth(*, as_of=None, include_contingent=False) -> Decimal:
    """Assets − liabilities (base) — the household's net worth.

    Contingent liabilities (the `2950` subtree: co-signed / guaranteed debts a household is only
    secondarily liable for and that someone else pays) are treated as off-balance-sheet and excluded
    by default — so a co-signed loan doesn't distort net worth even while it's fully tracked and
    shown. Pass `include_contingent=True` for the total-obligations view.
    """
    liabilities = _type_total(AccountType.LIABILITY, as_of=as_of)
    if not include_contingent:
        liabilities -= _contingent_liability_total(as_of=as_of)
    return _type_total(AccountType.ASSET, as_of=as_of) - liabilities


def party_balance(*, person=None, organization=None, as_of=None) -> Decimal:
    """Signed base total (debit − credit) of ledger lines with the given counterparty."""
    qs = _posted_lines(as_of)
    if person is not None:
        qs = qs.filter(person=person)
    if organization is not None:
        qs = qs.filter(organization=organization)
    return _raw(qs)
