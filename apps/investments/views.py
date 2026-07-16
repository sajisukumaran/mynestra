"""Investments views (tenant-scoped, member-accessible). Mirrors the Banking idiom: a dashboard, an
accounts list (search / group filter chips / sort / paginate), an account detail with a
Holdings / Register / Holders / History tab set, a holding drill-down (open lots), a securities
master, and popup (c-modal) forms. Every money movement posts to the ledger through
apps.investments.services; this layer only reads POST, calls the service, and redirects."""

import datetime
import json
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_date

from apps.contacts.models import Address, Person
from apps.finance.models import Account, Currency
from apps.finance.models import AccountType as GLType
from apps.finance.services import (
    base_currency,
    is_expert_mode,
    posting_map_for,
    set_posting_map,
)
from apps.investments.forms import InvestmentAccountForm, SecurityForm
from apps.investments.models import (
    CONTRIBUTION_TAX_YEAR_TYPES,
    REGISTRATION_GROUP,
    SECURITY_TYPES,
    AccountGroup,
    AssetClass,
    HsaCoverage,
    InvestmentAccount,
    InvestmentAccountHolder,
    InvestmentTransaction,
    InvTxnType,
    Lot,
    OptionRight,
    Registration,
    Security,
    SecurityKind,
    SecurityPrice,
    VestingGrant,
    VestingKind,
    VestingTranche,
)
from apps.investments.services import (
    POSTING_ACTIVITIES,
    allocation,
    apply_transaction,
    attach_account_totals,
    contribution_limit_status,
    contribution_summary,
    create_matching_leg,
    dashboard_stats,
    donut_segments,
    ensure_gl_account,
    holdings,
    income_summary,
    institution_row,
    institution_summary,
    line_chart_points,
    register_page,
    remove_transaction,
    repool_security,
    security_performance,
    sync_holder_p2o,
    transfer_totals,
    unvested_at_risk_total,
    upcoming_vesting,
    value_over_time,
    vesting_summary,
)
from apps.organizations.models import Branch, Organization
from apps.relationships.services import parse_partial_dates
from apps.setup.models import Category
from apps.tenants.models import Membership, Role

ACCOUNT_SORTS = {
    "nickname": ("nickname", "id"),
    "-nickname": ("-nickname", "-id"),
    "added": ("created_at", "id"),
    "-added": ("-created_at", "-id"),
}

# Registration codes per GL group, for the accounts-list group filter chips.
GROUP_REGISTRATIONS = {
    g: [r for r, gg in REGISTRATION_GROUP.items() if gg == g] for g in AccountGroup.values
}

# Transaction types offered in the register's "add" picker.
PICKER_TYPES = [
    (InvTxnType.BUY, "Buy"),
    (InvTxnType.SELL, "Sell"),
    (InvTxnType.CASH_IN_LIEU, "Cash in lieu (fractional)"),
    (InvTxnType.DIVIDEND, "Dividend"),
    (InvTxnType.DIVIDEND_REINVEST, "Dividend (reinvested)"),
    (InvTxnType.INTEREST, "Interest"),
    (InvTxnType.CAP_GAIN_DIST, "Capital gains distribution"),
    (InvTxnType.RETURN_OF_CAPITAL, "Return of capital"),
    (InvTxnType.CONTRIBUTION, "Contribution (money in)"),
    (InvTxnType.WITHDRAWAL, "Withdrawal (money out)"),
    (InvTxnType.TRANSFER_IN, "Transfer in"),
    (InvTxnType.TRANSFER_OUT, "Transfer out"),
    (InvTxnType.FEE, "Fee"),
    (InvTxnType.SPLIT, "Stock split"),
    (InvTxnType.IN_KIND_OUT, "In-kind transfer out"),
    (InvTxnType.IN_KIND_IN, "In-kind transfer in"),
    (InvTxnType.WORTHLESS, "Worthless write-off"),
    (InvTxnType.CASH_MERGER, "Cash buyout / merger"),
    (InvTxnType.MERGER, "Merger (stock-for-stock)"),
    (InvTxnType.SPINOFF, "Spin-off"),
    (InvTxnType.SELL_SHORT, "Sell short"),
    (InvTxnType.BUY_TO_COVER, "Buy to cover"),
    (InvTxnType.MARGIN_INTEREST, "Margin interest"),
    (InvTxnType.DIV_PAID_SHORT, "Dividend paid (short)"),
    (InvTxnType.OPENING, "Opening / existing holding"),
]


def tenant_url(request, path=""):
    return f"/t/{request.tenant.schema_name}/{path}"


def _is_owner(request) -> bool:
    return Membership.objects.filter(
        user=request.user, tenant=request.tenant, role=Role.OWNER
    ).exists()


def inv_context(request, active, **extra):
    ctx = {
        "active": active,
        "is_owner": _is_owner(request),
        "nav_accounts": InvestmentAccount.objects.count(),
        "nav_securities": Security.objects.count(),
        "nav_institutions": _brokerages().count(),
    }
    ctx.update(extra)
    return ctx


def _brokerages():
    """Organizations tagged with the system 'Brokerage' category (the institutions seam)."""
    return Organization.objects.filter(
        categories__kind="ORG", categories__name="Brokerage"
    ).distinct()


def _income_accounts():
    return Account.objects.filter(type=GLType.REVENUE, is_postable=True).order_by("code")


def _expense_accounts():
    return Account.objects.filter(type=GLType.EXPENSE, is_postable=True).order_by("code")


def _decimal(raw):
    """Parse a user-entered decimal, tolerating pasted formatting: thousands-separator commas,
    a leading currency symbol, and surrounding/embedded whitespace are stripped before parsing
    (e.g. "$1,234.56" → 1234.56). Anything still unparseable → None."""
    if raw is None:
        return None
    cleaned = "".join(ch for ch in str(raw) if not ch.isspace() and ch not in ",$£€¥")
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except (InvalidOperation, TypeError):
        return None


# --- Dashboard ------------------------------------------------------------------------------

VALUE_RANGES = [("3M", "3M"), ("1Y", "1Y"), ("ALL", "All")]


def _value_chart_ctx(request, range_key):
    """Context for the value-over-time chart (shared by the dashboard + the htmx range fragment)."""
    vot = value_over_time(range_key)
    geo = line_chart_points(
        vot["series"], min_v=vot["min"], max_v=vot["max"], start=vot["start"], end=vot["end"]
    )
    return {
        "ranges": VALUE_RANGES, "range": vot["range"], "line_geo": geo,
        "line_market": vot["last_market"], "line_invested": vot["last_invested"],
        "line_gain": vot["gain"], "base": base_currency(),
    }


def value_over_time_fragment(request):
    """htmx fragment: the value chart re-rendered for the chosen range (3M / 1Y / All)."""
    range_key = request.GET.get("range", "1Y")
    return render(request, "investments/partials/value_chart.html",
                  _value_chart_ctx(request, range_key))


def dashboard(request):
    stats = dashboard_stats()
    base = base_currency()
    donut = donut_segments(stats["allocation"])
    group_bars = [{"label": s.label, "value": s.value, "tint": s.tint} for s in stats["by_group"]]
    group_total = sum((b["value"] for b in group_bars), Decimal("0"))
    cost = stats["total_cost"]
    unrealized_pct = (stats["unrealized"] / cost * 100) if cost else Decimal("0")
    ctx = inv_context(
        request, "dashboard", base=base, donut=donut, group_bars=group_bars,
        group_total=group_total, unrealized_pct=unrealized_pct,
        gain_dir="up" if stats["unrealized"] >= 0 else "down",
        unvested_at_risk=unvested_at_risk_total(), upcoming_vesting=upcoming_vesting(), **stats,
    )
    ctx.update(_value_chart_ctx(request, "1Y"))  # initial paint (htmx swaps on range change)
    return render(request, "investments/dashboard.html", ctx)


# --- Accounts list --------------------------------------------------------------------------

def account_list(request):
    qs = InvestmentAccount.objects.select_related("institution", "currency")

    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(nickname__icontains=q)
            | Q(number__icontains=q)
            | Q(institution__name__icontains=q)
        ).distinct()

    group = request.GET.get("group", "")
    if group in AccountGroup.values:
        qs = qs.filter(registration__in=GROUP_REGISTRATIONS[group])

    sort = request.GET.get("sort", "nickname")
    if sort not in ACCOUNT_SORTS:
        sort = "nickname"
    qs = qs.order_by(*ACCOUNT_SORTS[sort])

    accounts = list(qs)
    all_accounts = list(InvestmentAccount.objects.all())
    counts = {g: sum(1 for a in all_accounts if a.group == g) for g in AccountGroup.values}
    group_chips = [
        {"val": val, "label": label, "count": counts[val]} for val, label in AccountGroup.choices
    ]
    page = Paginator(accounts, 12).get_page(request.GET.get("page"))

    ctx = inv_context(
        request, "accounts",
        page=page, accounts=attach_account_totals(page.object_list),
        q=q, group=group, sort=sort,
        sort_name_next="-nickname" if sort == "nickname" else "nickname",
        sort_added_next="-added" if sort == "added" else "added",
        total=len(all_accounts), group_chips=group_chips,
        base=base_currency(),
    )
    return render(request, "investments/account_list.html", ctx)


# --- Account create / edit / delete ---------------------------------------------------------

def _save_holders(request, account):
    ids = request.POST.getlist("holders")
    primary = request.POST.get("primary_holder", "")
    account.holders.all().delete()
    for person in Person.objects.filter(pk__in=ids):
        InvestmentAccountHolder.objects.create(
            account=account, person=person, is_primary=(str(person.pk) == primary)
        )


def _maybe_opening_cash(request, account):
    """Create the opening settlement-cash transaction on setup (skipped if one already exists)."""
    amount = _decimal(request.POST.get("opening_balance"))
    if amount is None or amount <= 0:
        return
    if account.transactions.filter(txn_type=InvTxnType.OPENING, security__isnull=True).exists():
        return
    on = parse_date(request.POST.get("opening_date", "") or "") or datetime.date.today()
    txn = InvestmentTransaction.objects.create(
        account=account, txn_type=InvTxnType.OPENING, date=on, amount=amount
    )
    apply_transaction(txn, user=request.user, is_new=True)


def account_create(request):
    account = InvestmentAccount()
    # Prefill the institution when arriving from a brokerage's detail page (?institution=<id>).
    if request.method == "GET":
        inst = Organization.objects.filter(
            pk=request.GET.get("institution") or 0,
            categories__kind="ORG", categories__name="Brokerage",
        ).first()
        if inst:
            account.institution = inst
    return _account_form(request, account, "create")


def account_edit(request, pk):
    return _account_form(request, get_object_or_404(InvestmentAccount, pk=pk), "edit")


def _expert_gl_choice(request):
    """Expert-mode GL-node choice for a NEW account: (parent header, existing account)."""
    gl_mode = request.POST.get("gl_mode", "auto")
    if gl_mode == "parent":
        parent = Account.objects.filter(
            pk=request.POST.get("gl_parent") or 0, is_postable=False, type=GLType.ASSET
        ).first()
        return parent, None
    if gl_mode == "existing":
        existing = Account.objects.filter(
            pk=request.POST.get("gl_existing") or 0, is_postable=True, type=GLType.ASSET,
            investment_account__isnull=True,
        ).first()
        return None, existing
    return None, None


def _save_posting_maps(request, account):
    for act in POSTING_ACTIVITIES:
        acct_id = request.POST.get(f"map_{act['key']}") or None
        chosen = (
            Account.objects.filter(pk=acct_id, is_postable=True).first() if acct_id else None
        )
        set_posting_map(account, act["key"], chosen)


def _resolve_institution(request, new_name, selected):
    """Return (institution, branch). Creates a Brokerage-category Organization from the inline
    'add a new institution' fields when given; else uses the selected org + its posted branch."""
    if not new_name:
        branch = (
            Branch.objects.filter(pk=request.POST.get("branch") or 0, organization=selected)
            .first()
            if selected
            else None
        )
        return selected, branch

    org = Organization.objects.create(name=new_name)
    org.categories.add(Category.objects.get(kind=Category.Kind.ORG, name="Brokerage"))
    branch = None
    city = request.POST.get("new_institution_city", "").strip()
    if city:
        Address.objects.create(organization=org, city=city, is_primary=True)
    return org, branch


def _account_form(request, account, mode):
    form = InvestmentAccountForm(request.POST or None, instance=account)
    expert = is_expert_mode()
    error = ""
    if request.method == "POST":
        new_name = request.POST.get("new_institution_name", "").strip()
        selected = Organization.objects.filter(pk=request.POST.get("institution") or 0).first()
        have_inst = bool(new_name or selected)
        registration = request.POST.get("registration") or Registration.TAXABLE_INDIVIDUAL
        currency = (
            Currency.objects.filter(code=request.POST.get("currency") or "").first()
            or base_currency()
        )
        if form.is_valid() and have_inst and registration in Registration.values:
            institution, branch = _resolve_institution(request, new_name, selected)
            account = form.save(commit=False)
            account.institution = institution
            account.branch = branch
            account.registration = registration
            account.currency = currency
            coverage = request.POST.get("hsa_coverage")
            account.hsa_coverage = (
                coverage if coverage in HsaCoverage.values else HsaCoverage.SELF_ONLY
            )
            for field, value in parse_partial_dates(request.POST, "opened", "closed").items():
                setattr(account, field, value)
            account.save()
            parent = existing = None
            if expert and mode == "create":
                parent, existing = _expert_gl_choice(request)
            ensure_gl_account(account, parent=parent, existing=existing)
            if expert:
                _save_posting_maps(request, account)
            _save_holders(request, account)
            sync_holder_p2o(account)
            _maybe_opening_cash(request, account)
            return redirect(tenant_url(request, f"investments/accounts/{account.pk}/"))
        if not have_inst:
            error = "Choose an institution or add a new one."

    people = Person.objects.filter(is_household_member=True)
    household_ids = set(people.values_list("pk", flat=True))
    current_holders = list(account.holders.select_related("person").all()) if account.pk else []
    selected_holders = {str(h.person_id): h.is_primary for h in current_holders}
    holder_extras = [
        {"id": h.person_id, "name": h.person.display_name,
         "tint": h.person.avatar_tint, "initials": h.person.initials}
        for h in current_holders
        if h.person_id not in household_ids
    ]
    primary_holder = next((str(h.person_id) for h in current_holders if h.is_primary), "")
    branches = (
        Branch.objects.filter(organization=account.institution)
        if account.institution_id
        else Branch.objects.none()
    )
    pmap = posting_map_for(account) if account.pk else {}
    posting_activities = [
        {**act, "current": pmap.get(act["key"], "")} for act in POSTING_ACTIVITIES
    ]
    ctx = inv_context(
        request, "accounts",
        form=form, account=account, mode=mode, error=error,
        institutions=_brokerages(),
        branches=branches,
        currencies=Currency.objects.filter(is_active=True),
        base=base_currency(),
        registrations=Registration.choices,
        hsa_coverages=HsaCoverage.choices,
        people=people,
        selected_holders=selected_holders,
        holder_extras=holder_extras,
        primary_holder=primary_holder,
        expert=expert,
        posting_activities=posting_activities,
        income_accounts=_income_accounts(),
        expense_accounts=_expense_accounts(),
        asset_headers=Account.objects.filter(
            is_postable=False, type=GLType.ASSET
        ).order_by("code"),
        adoptable_accounts=Account.objects.filter(
            is_postable=True, type=GLType.ASSET, is_system=False,
            investment_account__isnull=True,
        ).order_by("code"),
    )
    return render(request, "investments/account_form.html", ctx)


def account_delete(request, pk):
    account = get_object_or_404(InvestmentAccount, pk=pk)
    if request.method == "POST":
        account.delete()  # soft-delete → Setup → Recently deleted
    return redirect(tenant_url(request, "investments/accounts/"))


# --- Account detail (Holdings / Register / Holders / History) -------------------------------

def _register_sort_cols(sort, direction):
    """Per-column sort state for the register headers: the direction a header click should apply +
    the arrow to show. The active column toggles; others default to descending (ascending for the
    alphabetical Type column)."""
    cols = {}
    for key in ("date", "type", "cash", "balance"):
        if key == sort:
            cols[key] = {
                "active": True,
                "dir": "asc" if direction == "desc" else "desc",
                "arrow": "▲" if direction == "asc" else "▼",
            }
        else:
            cols[key] = {"active": False, "dir": "asc" if key == "type" else "desc", "arrow": ""}
    return cols


# Server-side header sorting for the fully-rendered (un-paginated) Holdings + Performance tables —
# same click-to-sort UX as the register, without its pagination. Each spec is (key, label, is_text);
# text columns default ascending, numeric columns descending. The `?tab=` + per-table sort params
# (hsort/hdir, psort/pdir) are carried on the header links so a sort returns to the right tab and
# never disturbs the other table's sort.
_HOLDINGS_SORT = (
    ("security", "Security", True),
    ("quantity", "Quantity", False),
    ("avg_cost", "Avg cost", False),
    ("price", "Price", False),
    ("market_value", "Market value", False),
    ("unrealized", "Unrealized", False),
)
_HOLDINGS_KEY = {
    "security": lambda h: (h.security.display or "").lower(),
    "quantity": lambda h: h.quantity,
    "avg_cost": lambda h: h.avg_cost,
    "price": lambda h: h.price,               # None (no quote) → sorts last, both directions
    "market_value": lambda h: h.market_value,
    "unrealized": lambda h: h.unrealized_gain,
}
_PERF_SORT = (
    ("instrument", "Instrument", True),
    ("bought", "Bought", False),
    ("sold", "Sold", False),
    ("qty", "Qty", False),
    ("cost", "Cost", False),
    ("fees", "Fees", False),
    ("dividends", "Dividends", False),
    ("interest", "Interest", False),
    ("amount_sold", "Amount sold", False),
    ("price", "Price", False),
    ("gain", "Gain", False),
    ("total_return", "Total return", False),
    ("return_pct", "Return %", False),
)
_PERF_KEY = {
    "instrument": lambda r: (r.security.display or "").lower(),
    "bought": lambda r: r.qty_bought,
    "sold": lambda r: r.qty_sold,
    "qty": lambda r: r.current_qty,
    "cost": lambda r: r.cost_basis,
    "fees": lambda r: r.fees,
    "dividends": lambda r: r.dividends,
    "interest": lambda r: r.interest,
    "amount_sold": lambda r: r.amount_sold,
    "price": lambda r: r.price,               # None → last
    "gain": lambda r: r.gain,
    "total_return": lambda r: r.total_return,
    "return_pct": lambda r: r.return_pct,     # None (income-only rows) → last
}


def _table_sort(specs, sort, direction, default):
    """Resolve a requested (sort, direction) against `specs` and build the header rows for the
    template. Falls back to `default` (its natural direction) when the column is unknown. Returns
    (sort, direction, headers) where each header carries its label, numeric flag, whether it's the
    active sort, the direction a click applies next, and the current-sort arrow."""
    columns = {key for key, _label, _text in specs}
    text_cols = {key for key, _label, is_text in specs if is_text}
    if sort not in columns:
        sort, direction = default, None
    if direction not in ("asc", "desc"):
        direction = "asc" if sort in text_cols else "desc"
    headers = []
    for key, label, is_text in specs:
        active = key == sort
        headers.append({
            "key": key, "label": label, "num": not is_text, "active": active,
            "dir": ("asc" if direction == "desc" else "desc") if active
                   else ("asc" if is_text else "desc"),
            "arrow": ("up" if direction == "asc" else "down") if active else "",
        })
    return sort, direction, headers


def _apply_sort(rows, keyfn, direction):
    """Stable sort of `rows` by `keyfn`; None keys always sort last (both directions)."""
    present = [r for r in rows if keyfn(r) is not None]
    missing = [r for r in rows if keyfn(r) is None]
    present.sort(key=keyfn, reverse=(direction == "desc"))
    return present + missing


def account_detail(request, pk):
    account = get_object_or_404(
        InvestmentAccount.objects.select_related("institution", "branch", "currency", "gl_account"),
        pk=pk,
    )
    attach_account_totals([account])  # header stats read stamped figures, not one query each
    hold = holdings(account)
    market = sum((h.market_value for h in hold), Decimal("0"))
    unrealized = sum((h.unrealized_gain for h in hold), Decimal("0"))
    # Holdings table: server-side header sort (defaults to market value, high → low, as before).
    hsort, _hdir, holdings_headers = _table_sort(
        _HOLDINGS_SORT, request.GET.get("hsort"), request.GET.get("hdir"), "market_value"
    )
    hold = _apply_sort(hold, _HOLDINGS_KEY[hsort], _hdir)
    vesting_rows, vesting_totals = vesting_summary(account)
    # Securities this account has ever transacted (held now or previously) — scopes the register's
    # Security picker for income / holding operations (dividend, sell, split, …) so it isn't the
    # whole instrument master. Acquisitions (buy / opening / in-kind-in / short/option open) still
    # offer every active security.
    sec_ids = set(
        account.transactions.filter(security__isnull=False).values_list("security_id", flat=True)
    ) | set(account.lots.values_list("security_id", flat=True))
    # Register: sorted + paginated so a large register renders (and reloads) fast. The active tab is
    # carried in ?tab= so a sort/page link lands the reader back on the Register tab.
    reg = register_page(
        account,
        sort=request.GET.get("sort", "date"),
        direction=request.GET.get("dir", "desc"),
        page=request.GET.get("page") or 1,
    )
    tab = request.GET.get("tab", "holdings")
    if tab not in ("holdings", "register", "performance", "holders", "vesting", "history"):
        tab = "holdings"
    # Performance table: server-side header sort (defaults to best % return first, as before).
    perf = security_performance(account)
    psort, _pdir, perf_headers = _table_sort(
        _PERF_SORT, request.GET.get("psort"), request.GET.get("pdir"), "return_pct"
    )
    perf["rows"] = _apply_sort(perf["rows"], _PERF_KEY[psort], _pdir)
    ctx = inv_context(
        request, "accounts",
        account=account,
        holdings=hold,
        holdings_headers=holdings_headers,
        vesting_rows=vesting_rows,
        vesting_totals=vesting_totals,
        market_total=market,
        unrealized_total=unrealized,
        register=reg,
        register_cols=_register_sort_cols(reg["sort"], reg["direction"]),
        register_arrow="up" if reg["direction"] == "asc" else "down",
        active_tab=tab,
        holders=list(account.holders.select_related("person").all()),
        history=account.history.all()[:60],
        base=base_currency(),
        picker_types=PICKER_TYPES,
        securities=Security.objects.filter(is_active=True).order_by("symbol", "name"),
        account_securities=Security.objects.filter(pk__in=sec_ids).order_by("symbol", "name"),
        # Held quantity per security → the cash-in-lieu picker defaults to the fractional remainder.
        held_qty_json=json.dumps({str(h.security.id): float(h.quantity) for h in hold}),
        income_accounts=_income_accounts(),
        expense_accounts=_expense_accounts(),
        bank_accounts=_bank_accounts(),
        investment_accounts=InvestmentAccount.objects.exclude(pk=account.pk).order_by("nickname"),
        contribution_rows=contribution_summary(account) if account.tracks_contribution_year else [],
        limit_status=contribution_limit_status(account),
        income=income_summary(account),
        transfers=transfer_totals(account),
        performance=perf,
        perf_headers=perf_headers,
    )
    return render(request, "investments/account_detail.html", ctx)


def _bank_accounts():
    from apps.banking.models import BankAccount

    return BankAccount.objects.select_related("bank").all()


# --- Institutions (brokerages — a grouped lens over the accounts) ---------------------------

def institution_list(request):
    """The Institutions index: every brokerage with its combined value, plus household-wide totals
    and a value-by-institution breakdown."""
    rows = institution_summary()
    total_value = sum((r["total_value"] for r in rows), Decimal("0"))
    total_market = sum((r["market"] for r in rows), Decimal("0"))
    total_cash = sum((r["cash"] for r in rows), Decimal("0"))
    account_count = sum(r["account_count"] for r in rows)
    bars = [
        {"label": r["org"].display, "value": r["total_value"], "tint": r["org"].avatar_tint}
        for r in rows if r["total_value"] > 0
    ]
    ctx = inv_context(
        request, "institutions",
        rows=rows, bars=bars, total_value=total_value, total_market=total_market,
        total_cash=total_cash, account_count=account_count, base=base_currency(),
    )
    return render(request, "investments/institution_list.html", ctx)


def institution_create(request):
    """Add a brokerage with minimal info (name + optional city / website); tags it Brokerage so it
    joins the institutions seam, then opens its detail page."""
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        if name:
            org = Organization.objects.create(
                name=name, website=request.POST.get("website", "").strip()
            )
            org.categories.add(Category.objects.get(kind=Category.Kind.ORG, name="Brokerage"))
            city = request.POST.get("city", "").strip()
            if city:
                Address.objects.create(organization=org, city=city, is_primary=True)
            return redirect(tenant_url(request, f"investments/institutions/{org.pk}/"))
    return redirect(tenant_url(request, "investments/institutions/"))


def institution_detail(request, org):
    """One brokerage: totals, its accounts (with per-account breakdown), an asset-class allocation
    over just its holdings, and its branches."""
    organization = get_object_or_404(_brokerages(), pk=org)
    accounts = attach_account_totals(
        organization.investment_accounts.select_related("currency", "branch")
    )
    summary = institution_row(organization, accounts)
    donut = donut_segments(allocation(accounts=accounts, by="asset_class"))
    performance = [
        {"account": a, "report": security_performance(a)}
        for a in accounts if a.transactions.exists()
    ]
    ctx = inv_context(
        request, "institutions",
        organization=organization, summary=summary, accounts=accounts, donut=donut,
        branches=list(organization.branches.all()), performance=performance,
        base=base_currency(),
    )
    return render(request, "investments/institution_detail.html", ctx)


def institution_edit(request, org):
    """Edit a brokerage's name / website / primary city inline from its detail page."""
    organization = get_object_or_404(_brokerages(), pk=org)
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        if name:
            organization.name = name
            organization.website = request.POST.get("website", "").strip()
            organization.save()
            city = request.POST.get("city", "").strip()
            addr = organization.addresses.filter(is_primary=True).first()
            if city and addr:
                addr.city = city
                addr.save()
            elif city:
                Address.objects.create(organization=organization, city=city, is_primary=True)
    return redirect(tenant_url(request, f"investments/institutions/{organization.pk}/"))


def branch_create(request, org):
    """Add a branch / office to a brokerage."""
    organization = get_object_or_404(_brokerages(), pk=org)
    if request.method == "POST":
        name = request.POST.get("branch_name", "").strip()
        if name:
            Branch.objects.create(
                organization=organization, name=name,
                number=request.POST.get("branch_number", "").strip(),
            )
    return redirect(tenant_url(request, f"investments/institutions/{organization.pk}/"))


def holding_detail(request, pk, sec):
    account = get_object_or_404(InvestmentAccount, pk=pk)
    security = get_object_or_404(Security, pk=sec)
    lots = account.lots.filter(security=security, open=True).order_by("acquired_date", "id")
    txns = account.transactions.filter(security=security).order_by("-date", "-id")
    match = next((h for h in holdings(account) if h.security.id == security.id), None)
    ctx = inv_context(
        request, "accounts",
        account=account, security=security, lots=lots, txns=txns, holding=match,
        base=base_currency(),
    )
    return render(request, "investments/holding_detail.html", ctx)


# --- Transactions ---------------------------------------------------------------------------

def _parse_lot_carry(request):
    """Build a `lot_carry` snapshot from the external in-kind-IN form's repeated lot rows
    (parallel lot_acquired / lot_qty / lot_cost inputs). Skips blank/invalid rows."""
    dates = request.POST.getlist("lot_acquired")
    qtys = request.POST.getlist("lot_qty")
    costs = request.POST.getlist("lot_cost")
    rows = []
    for d, q, c in zip(dates, qtys, costs, strict=False):
        pd = parse_date(d or "")
        qd = _decimal(q)
        cd = _decimal(c)
        if pd is None or qd is None or qd <= 0 or cd is None or cd < 0:
            continue
        rows.append({"acquired_date": pd.isoformat(), "quantity": str(qd), "cost": str(cd)})
    return rows


def _resolve_target_security(request, source):
    """The merger / spin-off target security Y: an existing pick, or created inline (symbol + name),
    inheriting the source security's currency + asset class. Returns None if neither is provided."""
    existing = Security.objects.filter(pk=request.POST.get("target_security") or 0).first()
    if existing:
        return existing
    symbol = (request.POST.get("new_target_symbol") or "").strip()
    name = (request.POST.get("new_target_name") or "").strip()
    if not (symbol or name):
        return None
    return Security.objects.create(
        symbol=symbol,
        name=name or symbol,
        kind=SecurityKind.STOCK,
        asset_class=source.asset_class if source else AssetClass.EQUITY,
        currency=source.currency if source else base_currency(),
    )


def _apply_txn_post(request, txn):
    """Populate a (new or existing) transaction from POST; save + return it, or None if invalid."""
    t = request.POST.get("txn_type", "")
    date = parse_date(request.POST.get("date", "") or "")
    if t not in InvTxnType.values or date is None:
        return None

    amount = _decimal(request.POST.get("amount")) or Decimal("0")
    fee = _decimal(request.POST.get("fee")) or Decimal("0")
    quantity = _decimal(request.POST.get("quantity")) or Decimal("0")
    price = _decimal(request.POST.get("price")) or Decimal("0")
    # No money/quantity figure is ever negative (a short uses a positive quantity; the engine signs
    # it). Reject rather than silently store nonsense.
    if amount < 0 or fee < 0 or quantity < 0 or price < 0:
        return None

    security = None
    if t in SECURITY_TYPES or t in (InvTxnType.DIVIDEND, InvTxnType.INTEREST,
                                    InvTxnType.CAP_GAIN_DIST, InvTxnType.OPENING):
        security = Security.objects.filter(pk=request.POST.get("security") or 0).first()

    lot_rows = _parse_lot_carry(request) if t == InvTxnType.IN_KIND_IN else []

    # Per-type required-field guards.
    if t in (InvTxnType.BUY, InvTxnType.SELL, InvTxnType.CASH_IN_LIEU,
             InvTxnType.DIVIDEND_REINVEST, InvTxnType.SELL_SHORT, InvTxnType.BUY_TO_COVER):
        if security is None or quantity <= 0 or amount <= 0:
            return None
    elif t == InvTxnType.DIV_PAID_SHORT:
        if security is None or amount <= 0:  # substitute dividend paid on the shorted security
            return None
    elif t in (InvTxnType.OPT_BUY_OPEN, InvTxnType.OPT_SELL_CLOSE,
               InvTxnType.OPT_SELL_OPEN, InvTxnType.OPT_BUY_CLOSE):
        contracts = _decimal(request.POST.get("contracts"))
        if (security is None or not security.is_option or not contracts or contracts <= 0
                or price <= 0):  # premium per share
            return None
    elif t in (InvTxnType.OPT_EXERCISE, InvTxnType.OPT_ASSIGN):
        contracts = _decimal(request.POST.get("contracts"))
        if (security is None or not security.is_option or security.strike is None
                or not security.option_right or security.underlying_id is None
                or not contracts or contracts <= 0):
            return None
    elif t == InvTxnType.SPLIT:
        if security is None:
            return None
    elif t == InvTxnType.RETURN_OF_CAPITAL:
        if security is None or amount <= 0:
            return None
    elif t == InvTxnType.OPENING:
        if amount <= 0:  # opening cash or opening-holding cost
            return None
    elif t == InvTxnType.IN_KIND_OUT:
        if security is None or quantity <= 0:  # shares leaving the account
            return None
    elif t == InvTxnType.IN_KIND_IN:
        if security is None or not lot_rows:  # user-entered incoming lots (external)
            return None
    elif t == InvTxnType.WORTHLESS:
        if security is None:  # whole position written off
            return None
    elif t == InvTxnType.CASH_MERGER:
        if security is None or amount <= 0:  # whole position bought out for cash
            return None
    elif t in (InvTxnType.MERGER, InvTxnType.SPINOFF):
        rn = _decimal(request.POST.get("split_ratio_new"))
        ro = _decimal(request.POST.get("split_ratio_old"))
        has_target = bool(
            (request.POST.get("target_security") or "").strip()
            or (request.POST.get("new_target_symbol") or "").strip()
            or (request.POST.get("new_target_name") or "").strip()
        )
        if security is None or not (rn and ro and rn > 0 and ro > 0) or not has_target:
            return None
        if t == InvTxnType.SPINOFF:
            # Optional: blank / 0 keeps all cost basis on the parent (the spun-off shares get $0
            # basis — a valid, conservative choice when the issuer's allocation isn't known yet).
            bp = _decimal(request.POST.get("basis_pct")) or Decimal("0")
            if bp < 0 or bp > 100:
                return None
            # Optional cash-in-lieu of the fractional share — never negative.
            if (_decimal(request.POST.get("cash_in_lieu")) or Decimal("0")) < 0:
                return None
    else:  # cash types
        if amount <= 0:
            return None

    txn.txn_type = t
    txn.date = date
    # Settlement date is informational and only meaningful on trades; cleared otherwise.
    txn.settlement_date = (
        parse_date(request.POST.get("settlement_date", "") or "")
        if t in (InvTxnType.BUY, InvTxnType.SELL, InvTxnType.SELL_SHORT, InvTxnType.BUY_TO_COVER)
        else None
    )
    # Dividend lifecycle dates — informational, dividend / reinvested-dividend only; cleared for any
    # other type so switching type never leaves a stale date.
    is_div = t in (InvTxnType.DIVIDEND, InvTxnType.DIVIDEND_REINVEST)
    for f in ("declaration_date", "ex_dividend_date", "record_date"):
        setattr(txn, f, parse_date(request.POST.get(f) or "") if is_div else None)
    txn.amount = amount
    txn.fee = fee if t in (InvTxnType.BUY, InvTxnType.SELL, InvTxnType.FEE,
                           InvTxnType.SELL_SHORT, InvTxnType.BUY_TO_COVER) else Decimal("0")
    txn.quantity = quantity
    txn.price = price
    txn.security = security
    txn.memo = request.POST.get("memo", "").strip()
    txn.reference = request.POST.get("reference", "").strip()
    txn.cleared = request.POST.get("cleared") in ("on", "1", "true")

    txn.split_ratio_new = txn.split_ratio_old = None
    txn.target_security = None
    txn.basis_pct = None
    if t == InvTxnType.SPLIT:
        txn.split_ratio_new = _decimal(request.POST.get("split_ratio_new"))
        txn.split_ratio_old = _decimal(request.POST.get("split_ratio_old"))
        if not (txn.split_ratio_new and txn.split_ratio_old
                and txn.split_ratio_new > 0 and txn.split_ratio_old > 0):
            return None
    elif t in (InvTxnType.MERGER, InvTxnType.SPINOFF):
        # Cash-neutral corporate action: X (`security`) → Y (`target_security`) at a share ratio.
        # (A spin-off may also bring in cash-in-lieu of a fractional share — stored in `amount`.)
        txn.split_ratio_new = _decimal(request.POST.get("split_ratio_new"))
        txn.split_ratio_old = _decimal(request.POST.get("split_ratio_old"))
        txn.target_security = _resolve_target_security(request, security)
        txn.amount = txn.quantity = txn.price = Decimal("0")
        if txn.target_security is None:
            return None
        if t == InvTxnType.SPINOFF:
            txn.basis_pct = _decimal(request.POST.get("basis_pct")) or Decimal("0")
            # Cash received in lieu of the fractional Y share; the engine sells that fraction for it
            # so the entitlement lands on whole shares + cash. Blank / 0 = keep the fraction.
            txn.amount = _decimal(request.POST.get("cash_in_lieu")) or Decimal("0")

    # Options: expand contracts → shares-equivalent once (quantity = contracts × multiplier) and
    # derive the cash amount. Open/close use the premium/share; exercise/assignment use the strike.
    if t in (InvTxnType.OPT_BUY_OPEN, InvTxnType.OPT_SELL_CLOSE, InvTxnType.OPT_SELL_OPEN,
             InvTxnType.OPT_BUY_CLOSE, InvTxnType.OPT_EXERCISE, InvTxnType.OPT_ASSIGN):
        contracts = _decimal(request.POST.get("contracts")) or Decimal("0")
        mult = security.multiplier or Decimal("100")
        txn.quantity = contracts * mult
        txn.fee = fee
        if t in (InvTxnType.OPT_EXERCISE, InvTxnType.OPT_ASSIGN):
            txn.price = security.strike
            txn.amount = security.strike * txn.quantity   # strike × shares
        else:
            txn.price = price                             # premium per share
            txn.amount = price * txn.quantity             # total premium

    txn.cost_basis_method = (
        request.POST.get("cost_basis_method")
        if t in (InvTxnType.SELL, InvTxnType.BUY_TO_COVER) else "fifo"
    )
    if txn.cost_basis_method not in ("fifo", "specific"):
        txn.cost_basis_method = "fifo"
    txn.lot_selection = None

    # Category override: an income account for a contribution (employer match), or an expense
    # account for a fee / margin interest / substitute dividend.
    txn.category_account = None
    if t in (InvTxnType.CONTRIBUTION, InvTxnType.FEE,
             InvTxnType.MARGIN_INTEREST, InvTxnType.DIV_PAID_SHORT):
        txn.category_account = Account.objects.filter(
            pk=request.POST.get("category_account") or 0, is_postable=True
        ).first()

    # Counterparty for a cash transfer: a tracked bank account (`bank:<pk>`) or another of the
    # household's investment accounts (`inv:<pk>`) from the unified picker; else external/untracked.
    txn.counter_account = None
    txn.counter_investment_account = None
    txn.counter_external = ""
    if t in (InvTxnType.TRANSFER_IN, InvTxnType.TRANSFER_OUT):
        from apps.banking.models import BankAccount

        kind, _, cid = (request.POST.get("counter") or "").strip().partition(":")
        if kind == "bank" and cid:
            txn.counter_account = BankAccount.objects.filter(pk=cid).first()
        elif kind == "inv" and cid and cid != str(txn.account_id):
            txn.counter_investment_account = InvestmentAccount.objects.filter(pk=cid).first()
        txn.counter_external = request.POST.get("counter_external", "").strip()

    # In-kind transfers / worthless / cash-merger. `lot_carry` is user-entered only for an external
    # in-kind IN; the OUT leg's snapshot is materialized by the engine on replay (never here). The
    # mirror IN leg of an internal transfer is managed by the service, not this form.
    if t == InvTxnType.IN_KIND_OUT:
        txn.amount = Decimal("0")
        txn.lot_carry = None
        dest_id = request.POST.get("counter_investment_account") or ""
        if dest_id and dest_id != str(txn.account_id):
            txn.counter_investment_account = InvestmentAccount.objects.filter(pk=dest_id).first()
    elif t == InvTxnType.IN_KIND_IN:
        txn.amount = Decimal("0")
        txn.quantity = sum((Decimal(e["quantity"]) for e in lot_rows), Decimal("0"))
        txn.lot_carry = lot_rows
    elif t == InvTxnType.WORTHLESS:
        txn.amount = Decimal("0")
        txn.quantity = Decimal("0")
        txn.lot_carry = None
    elif t == InvTxnType.CASH_MERGER:
        txn.quantity = Decimal("0")  # the engine disposes the whole position for the buyout cash
        txn.lot_carry = None

    txn.payee_person = None
    txn.payee_organization = None
    pid = request.POST.get("payee_person") or ""
    oid = request.POST.get("payee_organization") or ""
    if pid:
        txn.payee_person = Person.objects.filter(pk=pid).first()
    elif oid:
        txn.payee_organization = Organization.objects.filter(pk=oid).first()

    # Contribution tax year — only on year-tracked accounts (IRA/HSA/529) for contribution /
    # transfer-in; cleared otherwise so switching type or account never leaves a stale year.
    txn.tax_year = None
    if txn.account.tracks_contribution_year and t in CONTRIBUTION_TAX_YEAR_TYPES:
        ty = (request.POST.get("tax_year") or "").strip()
        if ty.isdigit():
            txn.tax_year = int(ty)

    txn.save()
    return txn


# Shown (as a toast) when a save is rejected, so an invalid entry is never silently dropped.
TXN_INVALID_MSG = (
    "Couldn't save the transaction. Check the required fields for its type — a date, and for a "
    "trade a security with a positive quantity and amount — and that no value is negative."
)


def txn_create(request, pk):
    account = get_object_or_404(InvestmentAccount, pk=pk)
    if request.method == "POST":
        txn = _apply_txn_post(request, InvestmentTransaction(account=account))
        if txn is not None:
            apply_transaction(txn, user=request.user, is_new=True)
            if (
                txn.txn_type in (InvTxnType.TRANSFER_IN, InvTxnType.TRANSFER_OUT)
                and (txn.counter_account_id or txn.counter_investment_account_id)
                and request.POST.get("auto_match")
            ):
                create_matching_leg(txn, user=request.user)
        else:
            messages.error(request, TXN_INVALID_MSG)
    return redirect(tenant_url(request, f"investments/accounts/{pk}/?tab=register"))


def txn_edit(request, pk, tx):
    account = get_object_or_404(InvestmentAccount, pk=pk)
    txn = get_object_or_404(InvestmentTransaction, pk=tx, account=account)
    # A managed mirror IN leg is maintained via its OUT leg — never edited directly.
    if txn.is_managed_in_leg:
        return redirect(tenant_url(request, f"investments/accounts/{pk}/?tab=register"))
    if request.method == "POST":
        if _apply_txn_post(request, txn) is not None:
            apply_transaction(txn, user=request.user, is_new=False)
        else:
            messages.error(request, TXN_INVALID_MSG)
    return redirect(tenant_url(request, f"investments/accounts/{pk}/?tab=register"))


def txn_delete(request, pk, tx):
    account = get_object_or_404(InvestmentAccount, pk=pk)
    txn = get_object_or_404(InvestmentTransaction, pk=tx, account=account)
    if request.method == "POST" and not txn.is_managed_in_leg:
        remove_transaction(txn, user=request.user)
    return redirect(tenant_url(request, f"investments/accounts/{pk}/?tab=register"))


def txn_toggle_cleared(request, pk, tx):
    account = get_object_or_404(InvestmentAccount, pk=pk)
    txn = get_object_or_404(InvestmentTransaction, pk=tx, account=account)
    if request.method == "POST":
        txn.cleared = not txn.cleared
        txn.save(update_fields=["cleared", "updated_at"])
    return redirect(tenant_url(request, f"investments/accounts/{pk}/?tab=register"))


# --- Vesting (employer match & equity grants; module-level overlay, no GL) -------------------

def _parse_tranches(request):
    """Build (vest_date, cumulative_percent) rows from the grant form's repeated inputs, validated
    unique-date + non-decreasing within (0, 100]. Returns the list, or None if empty/invalid."""
    dates = request.POST.getlist("tranche_date")
    pcts = request.POST.getlist("tranche_pct")
    rows = []
    seen = set()
    last = Decimal("0")
    for d, p in zip(dates, pcts, strict=False):
        vd = parse_date(d or "")
        pc = _decimal(p)
        if vd is None or pc is None:
            continue
        # reject duplicate dates, out-of-range %, or a non-monotonic (decreasing) schedule
        if vd in seen or pc <= 0 or pc > 100 or pc < last:
            return None
        seen.add(vd)
        rows.append((vd, pc))
        last = pc
    return rows or None


def _apply_vesting_post(request, grant):
    """Populate a (new or existing) vesting grant + replace its tranche schedule from POST; save +
    return it, or None if invalid."""
    kind = request.POST.get("kind", "")
    label = request.POST.get("label", "").strip()
    grant_date = parse_date(request.POST.get("grant_date", "") or "")
    total = _decimal(request.POST.get("total")) or Decimal("0")
    if kind not in VestingKind.values or not label or grant_date is None or total <= 0:
        return None

    security = None
    if kind == VestingKind.SHARES:
        security = Security.objects.filter(pk=request.POST.get("security") or 0).first()
        if security is None:
            return None  # a shares grant must name the security that vests

    tranches = _parse_tranches(request)
    if tranches is None:
        return None

    grant.kind = kind
    grant.label = label
    grant.grant_date = grant_date
    grant.total = total
    grant.security = security
    grant.funded = request.POST.get("funded") in ("on", "1", "true")
    grant.notes = request.POST.get("notes", "").strip()
    grant.save()

    grant.tranches.all().delete()
    VestingTranche.objects.bulk_create([
        VestingTranche(grant=grant, vest_date=vd, cumulative_percent=pc) for vd, pc in tranches
    ])
    return grant


def vesting_create(request, pk):
    account = get_object_or_404(InvestmentAccount, pk=pk)
    if request.method == "POST":
        _apply_vesting_post(request, VestingGrant(account=account))
    return redirect(tenant_url(request, f"investments/accounts/{pk}/?tab=vesting"))


def vesting_edit(request, pk, vid):
    account = get_object_or_404(InvestmentAccount, pk=pk)
    grant = get_object_or_404(VestingGrant, pk=vid, account=account)
    if request.method == "POST":
        _apply_vesting_post(request, grant)
    return redirect(tenant_url(request, f"investments/accounts/{pk}/?tab=vesting"))


def vesting_delete(request, pk, vid):
    account = get_object_or_404(InvestmentAccount, pk=pk)
    grant = get_object_or_404(VestingGrant, pk=vid, account=account)
    if request.method == "POST":
        grant.delete()  # soft delete
    return redirect(tenant_url(request, f"investments/accounts/{pk}/?tab=vesting"))


# --- Securities (instrument master) ---------------------------------------------------------

def security_list(request):
    qs = Security.objects.all()
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(symbol__icontains=q) | Q(name__icontains=q))
    kind = request.GET.get("kind", "")
    if kind in SecurityKind.values:
        qs = qs.filter(kind=kind)
    qs = qs.order_by("symbol", "name")
    page = Paginator(qs, 20).get_page(request.GET.get("page"))
    ctx = inv_context(
        request, "securities",
        page=page, securities=page.object_list, q=q, kind=kind,
        kinds=SecurityKind.choices, total=Security.objects.count(), base=base_currency(),
    )
    return render(request, "investments/security_list.html", ctx)


def security_mass_price(request):
    """Bulk price entry: one row per quotable instrument (active, minus CDs / money-market — those
    are valued by face/APR or stable-$1, not marked to market). A single as-of date + source apply
    to all rows. A blank row is left untouched; a filled row creates or overwrites (idempotent) that
    instrument's price on the date. Market marks only — no GL or lot effect."""
    quotable = (
        Security.objects.filter(is_active=True)
        .exclude(kind__in=[SecurityKind.CD, SecurityKind.MONEY_MARKET])
        .order_by("symbol", "name")
    )
    if request.method == "POST":
        as_of = parse_date(request.POST.get("as_of", "") or "") or datetime.date.today()
        source = request.POST.get("source", "").strip()
        updated = 0
        for sec in quotable:
            price = _decimal(request.POST.get(f"price_{sec.pk}"))
            if price is None or price < 0:
                continue  # blank / invalid → leave this instrument untouched on this date
            SecurityPrice.objects.update_or_create(
                security=sec, as_of=as_of, defaults={"price": price, "source": source}
            )
            updated += 1
        messages.success(
            request,
            f"Updated {updated} price{'' if updated == 1 else 's'} as of {as_of:%d %b %Y}."
            if updated else "No prices entered — nothing was updated.",
        )
        return redirect(tenant_url(request, "investments/securities/"))

    as_of = parse_date(request.GET.get("as_of", "") or "") or datetime.date.today()
    rows = [{"security": sec, "last": sec.prices.order_by("-as_of").first()} for sec in quotable]
    ctx = inv_context(
        request, "securities",
        rows=rows, as_of=as_of, source=request.GET.get("source", ""),
        total=len(rows), base=base_currency(),
    )
    return render(request, "investments/security_mass_price.html", ctx)


def security_create(request):
    return _security_form(request, Security(), "create")


def security_edit(request, pk):
    return _security_form(request, get_object_or_404(Security, pk=pk), "edit")


def _security_form(request, security, mode):
    was_tracking = security.track_lots  # DB value; the form mutates the instance on validate
    form = SecurityForm(request.POST or None, instance=security)
    if request.method == "POST":
        currency = (
            Currency.objects.filter(code=request.POST.get("currency") or "").first()
            or base_currency()
        )
        if form.is_valid():
            security = form.save(commit=False)
            security.currency = currency
            if security.kind == SecurityKind.OPTION:
                security.multiplier = _decimal(request.POST.get("multiplier")) or Decimal("100")
            security.save()
            # Toggling lot tracking re-pools (or un-pools) every account that holds it.
            if mode == "edit" and was_tracking != security.track_lots:
                repool_security(security, user=request.user)
            return redirect(tenant_url(request, f"investments/securities/{security.pk}/"))
    ctx = inv_context(
        request, "securities",
        form=form, security=security, mode=mode,
        currencies=Currency.objects.filter(is_active=True),
        base=base_currency(),
        kinds=SecurityKind.choices,
        asset_classes=AssetClass.choices,
        rights=OptionRight.choices,
        underlyings=Security.objects.exclude(kind=SecurityKind.OPTION).order_by("symbol", "name"),
    )
    return render(request, "investments/security_form.html", ctx)


def security_detail(request, pk):
    security = get_object_or_404(Security, pk=pk)
    prices = security.prices.order_by("-as_of")[:30]
    # Where this security is held (open lots grouped by account).
    positions = {}
    for lot in Lot.objects.filter(security=security, open=True).select_related("account"):
        p = positions.setdefault(
            lot.account_id, {"account": lot.account, "qty": Decimal("0"), "cost": Decimal("0")}
        )
        p["qty"] += lot.remaining_quantity
        p["cost"] += lot.cost_basis
    ctx = inv_context(
        request, "securities",
        security=security, prices=prices, positions=list(positions.values()),
        base=base_currency(), today=datetime.date.today(),
    )
    return render(request, "investments/security_detail.html", ctx)


def security_price(request, pk):
    security = get_object_or_404(Security, pk=pk)
    if request.method == "POST":
        price = _decimal(request.POST.get("price"))
        as_of = parse_date(request.POST.get("as_of", "") or "") or datetime.date.today()
        if price is not None and price >= 0:
            SecurityPrice.objects.update_or_create(
                security=security, as_of=as_of,
                defaults={"price": price, "source": request.POST.get("source", "").strip()},
            )
    return redirect(tenant_url(request, f"investments/securities/{pk}/"))


def security_price_edit(request, pk, price_id):
    """Correct a mistaken price mark (value / date / source). Moving it onto a date another mark
    already holds overwrites that one and drops this row (the (security, as_of) unique rule). Prices
    are market-value marks only — no GL or lot effect, so no rebuild."""
    security = get_object_or_404(Security, pk=pk)
    row = get_object_or_404(SecurityPrice, pk=price_id, security=security)
    if request.method == "POST":
        price = _decimal(request.POST.get("price"))
        as_of = parse_date(request.POST.get("as_of", "") or "") or row.as_of
        source = request.POST.get("source", "").strip()
        if price is not None and price >= 0:
            clash = (
                SecurityPrice.objects.filter(security=security, as_of=as_of)
                .exclude(pk=row.pk).first()
            )
            if clash is not None:
                clash.price = price
                clash.source = source
                clash.save(update_fields=["price", "source", "updated_at"])
                row.delete()
            else:
                row.as_of = as_of
                row.price = price
                row.source = source
                row.save(update_fields=["as_of", "price", "source", "updated_at"])
    return redirect(tenant_url(request, f"investments/securities/{pk}/"))


def security_price_delete(request, pk, price_id):
    security = get_object_or_404(Security, pk=pk)
    if request.method == "POST":
        SecurityPrice.objects.filter(pk=price_id, security=security).delete()
    return redirect(tenant_url(request, f"investments/securities/{pk}/"))


def security_rename(request, pk):
    """Ticker / symbol change: the same security under a new symbol (no lot/basis/cash effect).
    Keeps the Security row + all its lots; records a dated note. simple-history logs the change."""
    security = get_object_or_404(Security, pk=pk)
    if request.method == "POST":
        new_symbol = (request.POST.get("new_symbol") or "").strip()
        new_name = (request.POST.get("new_name") or "").strip()
        effective = parse_date(request.POST.get("effective_date", "") or "")
        if new_symbol and new_symbol != security.symbol:
            old_symbol = security.symbol or security.name
            when = (effective or datetime.date.today()).isoformat()
            note = f"Ticker changed {old_symbol} → {new_symbol} effective {when}."
            security.symbol = new_symbol
            if new_name:
                security.name = new_name
            security.notes = f"{note}\n{security.notes}".strip() if security.notes else note
            security.save()
    return redirect(tenant_url(request, f"investments/securities/{pk}/"))


def security_delete(request, pk):
    security = get_object_or_404(Security, pk=pk)
    if request.method == "POST" and not security.lots.filter(open=True).exists():
        security.delete()  # soft-delete
        return redirect(tenant_url(request, "investments/securities/"))
    return redirect(tenant_url(request, f"investments/securities/{pk}/"))


# --- htmx fragments -------------------------------------------------------------------------

def payee_search(request):
    q = request.GET.get("q", "").strip()
    # People on the other side of a transaction are household members only (you, family,
    # dependents) — not external business contacts. Organizations stay unfiltered, since a
    # payee here is often a company (dividend/interest/employer contribution).
    people = Person.objects.filter(is_household_member=True)
    orgs = Organization.objects.all()
    if q:
        people = people.filter(
            Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(preferred_name__icontains=q)
        )
        orgs = orgs.filter(Q(name__icontains=q) | Q(display_name__icontains=q))
    return render(
        request,
        "investments/partials/payee_search.html",
        {"people": people[:6], "orgs": orgs[:6], "q": q},
    )


def holder_search(request):
    q = request.GET.get("q", "").strip()
    people = Person.objects.all()
    if q:
        people = people.filter(
            Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(preferred_name__icontains=q)
        )
    return render(
        request, "investments/partials/holder_search.html", {"candidates": people[:8], "q": q}
    )


def branch_options(request):
    branches = Branch.objects.filter(organization_id=request.GET.get("institution") or 0)
    return render(request, "investments/partials/branch_options.html", {"branches": branches})
