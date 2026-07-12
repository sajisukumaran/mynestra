"""Investments views (tenant-scoped, member-accessible). Mirrors the Banking idiom: a dashboard, an
accounts list (search / group filter chips / sort / paginate), an account detail with a
Holdings / Register / Holders / History tab set, a holding drill-down (open lots), a securities
master, and popup (c-modal) forms. Every money movement posts to the ledger through
apps.investments.services; this layer only reads POST, calls the service, and redirects."""

import datetime
from decimal import Decimal, InvalidOperation

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
    REGISTRATION_GROUP,
    SECURITY_TYPES,
    AccountGroup,
    AssetClass,
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
    apply_transaction,
    create_matching_leg,
    dashboard_stats,
    donut_segments,
    ensure_gl_account,
    holdings,
    line_chart_points,
    register,
    remove_transaction,
    sync_holder_p2o,
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
    try:
        return Decimal((raw or "").strip())
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
        page=page, accounts=page.object_list, q=q, group=group, sort=sort,
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
    return _account_form(request, InvestmentAccount(), "create")


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

def account_detail(request, pk):
    account = get_object_or_404(
        InvestmentAccount.objects.select_related("institution", "branch", "currency", "gl_account"),
        pk=pk,
    )
    hold = holdings(account)
    market = sum((h.market_value for h in hold), Decimal("0"))
    vesting_rows, vesting_totals = vesting_summary(account)
    ctx = inv_context(
        request, "accounts",
        account=account,
        holdings=hold,
        vesting_rows=vesting_rows,
        vesting_totals=vesting_totals,
        market_total=market,
        rows=register(account),
        holders=list(account.holders.select_related("person").all()),
        history=account.history.all()[:60],
        base=base_currency(),
        picker_types=PICKER_TYPES,
        securities=Security.objects.filter(is_active=True).order_by("symbol", "name"),
        income_accounts=_income_accounts(),
        expense_accounts=_expense_accounts(),
        bank_accounts=_bank_accounts(),
        investment_accounts=InvestmentAccount.objects.exclude(pk=account.pk).order_by("nickname"),
    )
    return render(request, "investments/account_detail.html", ctx)


def _bank_accounts():
    from apps.banking.models import BankAccount

    return BankAccount.objects.select_related("bank").all()


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

    security = None
    if t in SECURITY_TYPES or t in (InvTxnType.DIVIDEND, InvTxnType.INTEREST,
                                    InvTxnType.CAP_GAIN_DIST, InvTxnType.OPENING):
        security = Security.objects.filter(pk=request.POST.get("security") or 0).first()

    lot_rows = _parse_lot_carry(request) if t == InvTxnType.IN_KIND_IN else []

    # Per-type required-field guards.
    if t in (InvTxnType.BUY, InvTxnType.SELL, InvTxnType.DIVIDEND_REINVEST,
             InvTxnType.SELL_SHORT, InvTxnType.BUY_TO_COVER):
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
            bp = _decimal(request.POST.get("basis_pct"))
            if bp is None or bp <= 0 or bp > 100:
                return None
    else:  # cash types
        if amount <= 0:
            return None

    txn.txn_type = t
    txn.date = date
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
        txn.split_ratio_new = _decimal(request.POST.get("split_ratio_new"))
        txn.split_ratio_old = _decimal(request.POST.get("split_ratio_old"))
        txn.target_security = _resolve_target_security(request, security)
        txn.amount = txn.quantity = txn.price = Decimal("0")
        if txn.target_security is None:
            return None
        if t == InvTxnType.SPINOFF:
            txn.basis_pct = _decimal(request.POST.get("basis_pct"))

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

    txn.counter_account = None
    txn.counter_external = ""
    if t in (InvTxnType.TRANSFER_IN, InvTxnType.TRANSFER_OUT):
        from apps.banking.models import BankAccount

        txn.counter_account = BankAccount.objects.filter(
            pk=request.POST.get("counter_account") or 0
        ).first()
        txn.counter_external = request.POST.get("counter_external", "").strip()

    # In-kind transfers / worthless / cash-merger. `lot_carry` is user-entered only for an external
    # in-kind IN; the OUT leg's snapshot is materialized by the engine on replay (never here). The
    # mirror IN leg of an internal transfer is managed by the service, not this form.
    txn.counter_investment_account = None
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

    txn.save()
    return txn


def txn_create(request, pk):
    account = get_object_or_404(InvestmentAccount, pk=pk)
    if request.method == "POST":
        txn = _apply_txn_post(request, InvestmentTransaction(account=account))
        if txn is not None:
            apply_transaction(txn, user=request.user, is_new=True)
            if (
                txn.txn_type in (InvTxnType.TRANSFER_IN, InvTxnType.TRANSFER_OUT)
                and txn.counter_account_id
                and request.POST.get("auto_match")
            ):
                create_matching_leg(txn, user=request.user)
    return redirect(tenant_url(request, f"investments/accounts/{pk}/"))


def txn_edit(request, pk, tx):
    account = get_object_or_404(InvestmentAccount, pk=pk)
    txn = get_object_or_404(InvestmentTransaction, pk=tx, account=account)
    # A managed mirror IN leg is maintained via its OUT leg — never edited directly.
    if txn.is_managed_in_leg:
        return redirect(tenant_url(request, f"investments/accounts/{pk}/"))
    if request.method == "POST" and _apply_txn_post(request, txn) is not None:
        apply_transaction(txn, user=request.user, is_new=False)
    return redirect(tenant_url(request, f"investments/accounts/{pk}/"))


def txn_delete(request, pk, tx):
    account = get_object_or_404(InvestmentAccount, pk=pk)
    txn = get_object_or_404(InvestmentTransaction, pk=tx, account=account)
    if request.method == "POST" and not txn.is_managed_in_leg:
        remove_transaction(txn, user=request.user)
    return redirect(tenant_url(request, f"investments/accounts/{pk}/"))


def txn_toggle_cleared(request, pk, tx):
    account = get_object_or_404(InvestmentAccount, pk=pk)
    txn = get_object_or_404(InvestmentTransaction, pk=tx, account=account)
    if request.method == "POST":
        txn.cleared = not txn.cleared
        txn.save(update_fields=["cleared", "updated_at"])
    return redirect(tenant_url(request, f"investments/accounts/{pk}/"))


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
    return redirect(tenant_url(request, f"investments/accounts/{pk}/"))


def vesting_edit(request, pk, vid):
    account = get_object_or_404(InvestmentAccount, pk=pk)
    grant = get_object_or_404(VestingGrant, pk=vid, account=account)
    if request.method == "POST":
        _apply_vesting_post(request, grant)
    return redirect(tenant_url(request, f"investments/accounts/{pk}/"))


def vesting_delete(request, pk, vid):
    account = get_object_or_404(InvestmentAccount, pk=pk)
    grant = get_object_or_404(VestingGrant, pk=vid, account=account)
    if request.method == "POST":
        grant.delete()  # soft delete
    return redirect(tenant_url(request, f"investments/accounts/{pk}/"))


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


def security_create(request):
    return _security_form(request, Security(), "create")


def security_edit(request, pk):
    return _security_form(request, get_object_or_404(Security, pk=pk), "edit")


def _security_form(request, security, mode):
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
    people = Person.objects.all()
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
