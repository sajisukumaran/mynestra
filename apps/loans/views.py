"""Loans & Liabilities views (tenant-scoped, member-accessible). Mirrors the Banking/Cards idiom:
a consolidated dashboard, a loans list (search / type chips / sort / paginate), a loan detail with a
register + amortization schedule + borrowers + history tabs, and popup (c-modal) forms. Every money
movement posts to the ledger through apps.loans.services; this layer reads POST, calls the service,
and redirects. Amortization/payoff are pure-read overlays."""

import datetime
from decimal import Decimal, InvalidOperation

from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_date

from apps.contacts.models import Person
from apps.finance.models import Account, Currency
from apps.finance.models import AccountType as GLType
from apps.finance.services import (
    base_currency,
    is_expert_mode,
    posting_map_for,
    set_posting_map,
)
from apps.loans.amortization import payoff_projection, suggest_split
from apps.loans.forms import LoanForm
from apps.loans.models import (
    BorrowerRole,
    Funding,
    Loan,
    LoanBorrower,
    LoanRateChange,
    LoanTransaction,
    LoanTxnType,
    LoanType,
    RateType,
)
from apps.loans.services import (
    POSTING_ACTIVITIES,
    by_type_segments,
    contributions_by_borrower,
    create_matching_leg,
    dashboard_stats,
    delete_transaction,
    ensure_gl_account,
    interest_by_year,
    lender_bars,
    loan_chart_points,
    loan_value_series,
    payments_due,
    post_transaction,
    rate_on,
    register,
    repost_transaction,
    sync_borrower_p2o,
)
from apps.organizations.models import Organization
from apps.tenants.models import Membership, Role

LOAN_SORTS = {
    "nickname": ("nickname", "id"),
    "-nickname": ("-nickname", "-id"),
    "added": ("created_at", "id"),
    "-added": ("-created_at", "-id"),
}

# Types offered in the register's "add transaction" picker (opening/disbursement come from setup).
PICKER_TYPES = [
    (LoanTxnType.PAYMENT, "Payment"),
    (LoanTxnType.EXTRA_PRINCIPAL, "Extra principal"),
    (LoanTxnType.DRAW, "Draw"),
    (LoanTxnType.INTEREST, "Interest"),
    (LoanTxnType.FEE, "Fee"),
    (LoanTxnType.ADJUSTMENT, "Balance adjustment"),
]


def tenant_url(request, path=""):
    return f"/t/{request.tenant.schema_name}/{path}"


def _is_owner(request) -> bool:
    return Membership.objects.filter(
        user=request.user, tenant=request.tenant, role=Role.OWNER
    ).exists()


def loans_context(request, active, **extra):
    ctx = {
        "active": active,
        "is_owner": _is_owner(request),
        "nav_loans": Loan.objects.count(),
    }
    ctx.update(extra)
    return ctx


def _expense_accounts():
    return Account.objects.filter(type=GLType.EXPENSE, is_postable=True).order_by("code")


def _decimal(raw):
    try:
        return Decimal((raw or "").strip())
    except (InvalidOperation, TypeError):
        return None


def _int(raw):
    raw = (raw or "").strip()
    return int(raw) if raw.isdigit() else None


def _bank_accounts():
    from apps.banking.models import BankAccount

    return BankAccount.objects.select_related("bank").all()


# --- Dashboard ------------------------------------------------------------------------------

def dashboard(request):
    stats = dashboard_stats()
    loans = stats["loans"]
    donut, donut_total = by_type_segments(loans)
    bars, bars_total = lender_bars(loans)
    recent = list(
        LoanTransaction.objects.select_related("loan").order_by("-date", "-id")[:8]
    )
    ctx = loans_context(
        request, "dashboard", base=base_currency(),
        donut_segments=donut, donut_total=donut_total,
        bar_items=bars, bar_total=bars_total,
        due=payments_due(), recent=recent, **stats,
    )
    return render(request, "loans/dashboard.html", ctx)


# --- Loans list -----------------------------------------------------------------------------

def loan_list(request):
    qs = Loan.objects.select_related(
        "currency", "gl_account", "lender_person", "lender_organization"
    )
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(nickname__icontains=q)
            | Q(account_number__icontains=q)
            | Q(lender_organization__name__icontains=q)
            | Q(lender_person__first_name__icontains=q)
            | Q(lender_person__last_name__icontains=q)
        ).distinct()

    ltype = request.GET.get("type", "")
    if ltype in LoanType.values:
        qs = qs.filter(loan_type=ltype)

    sort = request.GET.get("sort", "nickname")
    if sort not in LOAN_SORTS:
        sort = "nickname"
    qs = qs.order_by(*LOAN_SORTS[sort])

    total = Loan.objects.count()
    type_chips = [
        {"val": val, "label": label, "count": Loan.objects.filter(loan_type=val).count()}
        for val, label in LoanType.choices
    ]
    page = Paginator(qs, 12).get_page(request.GET.get("page"))
    ctx = loans_context(
        request, "loans",
        page=page, loans=list(page.object_list), q=q, type=ltype, sort=sort,
        sort_name_next="-nickname" if sort == "nickname" else "nickname",
        sort_added_next="-added" if sort == "added" else "added",
        total=total, type_chips=type_chips, base=base_currency(),
    )
    return render(request, "loans/loan_list.html", ctx)


# --- Loan create / edit / delete ------------------------------------------------------------

def _save_borrowers(request, loan):
    pids = request.POST.getlist("borrower_person")
    roles = request.POST.getlist("borrower_role")
    loan.borrowers.all().delete()
    seen = set()
    for pid, role in zip(pids, roles, strict=False):
        if not pid or pid in seen:
            continue
        seen.add(pid)
        person = Person.objects.filter(pk=pid).first()
        if person is None:
            continue
        LoanBorrower.objects.create(
            loan=loan, person=person,
            role=role if role in BorrowerRole.values else BorrowerRole.CO_BORROWER,
        )


def _save_posting_maps(request, loan):
    for act in POSTING_ACTIVITIES:
        acct_id = request.POST.get(f"map_{act['key']}") or None
        chosen = Account.objects.filter(pk=acct_id, is_postable=True).first() if acct_id else None
        set_posting_map(loan, act["key"], chosen)


def _maybe_opening(request, loan):
    """On setup, post the opening balance owed or a disbursement (once)."""
    amount = _decimal(request.POST.get("opening_amount"))
    if amount is None or amount <= 0:
        return
    if loan.transactions.filter(
        txn_type__in=[LoanTxnType.OPENING, LoanTxnType.DISBURSEMENT]
    ).exists():
        return
    on = parse_date(request.POST.get("opening_date", "") or "") or datetime.date.today()
    if request.POST.get("start_mode") == "disburse":
        from apps.banking.models import BankAccount

        account = BankAccount.objects.filter(pk=request.POST.get("opening_account") or 0).first()
        txn = LoanTransaction.objects.create(
            loan=loan, txn_type=LoanTxnType.DISBURSEMENT, date=on, amount=amount,
            funding_source=Funding.BANK if account else Funding.EXTERNAL, funding_account=account,
        )
        post_transaction(txn, user=request.user)
        create_matching_leg(txn, user=request.user)
    else:
        txn = LoanTransaction.objects.create(
            loan=loan, txn_type=LoanTxnType.OPENING, date=on, amount=amount
        )
        post_transaction(txn, user=request.user)


def _apply_terms(request, loan, loan_type):
    """Set the term/rate/revolving fields from POST, clearing what doesn't apply to the type."""
    loan.annual_rate = _decimal(request.POST.get("annual_rate"))
    loan.rate_type = (
        request.POST.get("rate_type")
        if request.POST.get("rate_type") in RateType.values
        else RateType.FIXED
    )
    is_revolving = loan_type in {LoanType.HELOC, LoanType.LINE_OF_CREDIT}
    is_installment = loan_type in {
        LoanType.MORTGAGE, LoanType.AUTO, LoanType.STUDENT, LoanType.PERSONAL
    }
    if is_installment:
        loan.principal_original = _decimal(request.POST.get("principal_original"))
        loan.term_months = _int(request.POST.get("term_months"))
        loan.payment_amount = _decimal(request.POST.get("payment_amount"))
        loan.escrow_amount = _decimal(request.POST.get("escrow_amount")) or Decimal("0")
        loan.payment_day = _int(request.POST.get("payment_day"))
        loan.start_date = parse_date(request.POST.get("start_date") or "") or None
        loan.first_payment_date = parse_date(request.POST.get("first_payment_date") or "") or None
        freq = request.POST.get("payment_frequency")
        if freq in dict(loan._meta.get_field("payment_frequency").choices):
            loan.payment_frequency = freq
        loan.credit_limit = None
    elif is_revolving:
        loan.credit_limit = _decimal(request.POST.get("credit_limit"))
        loan.principal_original = loan.term_months = loan.payment_amount = None
        loan.escrow_amount = Decimal("0")
        loan.payment_day = None
        loan.start_date = loan.first_payment_date = None
    else:  # other liability
        loan.principal_original = _decimal(request.POST.get("principal_original"))
        loan.term_months = loan.payment_amount = loan.credit_limit = None
        loan.escrow_amount = Decimal("0")
        loan.payment_day = None
        loan.start_date = loan.first_payment_date = None


def loan_create(request):
    return _loan_form(request, Loan(), "create")


def loan_edit(request, pk):
    return _loan_form(request, get_object_or_404(Loan, pk=pk), "edit")


def _loan_form(request, loan, mode):
    form = LoanForm(request.POST or None, instance=loan)
    expert = is_expert_mode()
    error = ""
    if request.method == "POST":
        loan_type = request.POST.get("loan_type") or LoanType.PERSONAL
        currency = (
            Currency.objects.filter(code=request.POST.get("currency") or "").first()
            or base_currency()
        )
        lender_person = Person.objects.filter(pk=request.POST.get("lender_person") or 0).first()
        lender_org = Organization.objects.filter(
            pk=request.POST.get("lender_organization") or 0
        ).first()
        if lender_person and lender_org:  # exactly-one guard (prefer person if both slip through)
            lender_org = None
        if form.is_valid() and loan_type in LoanType.values:
            loan = form.save(commit=False)
            loan.loan_type = loan_type
            loan.currency = currency
            loan.lender_person = lender_person
            loan.lender_organization = lender_org
            loan.counts_toward_net_worth = request.POST.get("counts_toward_net_worth") in (
                "on", "1", "true",
            )
            _apply_terms(request, loan, loan_type)
            from apps.relationships.services import parse_partial_dates

            for field, value in parse_partial_dates(request.POST, "opened", "closed").items():
                setattr(loan, field, value)
            loan.save()
            ensure_gl_account(loan)  # reconciles the parent to the net-worth flag on edit
            if expert:
                _save_posting_maps(request, loan)
            _save_borrowers(request, loan)
            sync_borrower_p2o(loan)
            if mode == "create":
                _maybe_opening(request, loan)
            return redirect(tenant_url(request, f"loans/{loan.pk}/"))
        error = "Please complete the required fields."

    people = Person.objects.filter(is_household_member=True)
    current = list(loan.borrowers.select_related("person").all()) if loan.pk else []
    borrower_rows = [
        {"id": b.person_id, "name": b.person.display_name, "tint": b.person.avatar_tint,
         "initials": b.person.initials, "role": b.role}
        for b in current
    ]
    pmap = posting_map_for(loan) if loan.pk else {}
    posting_activities = [
        {**act, "current": pmap.get(act["key"], "")} for act in POSTING_ACTIVITIES
    ]
    ctx = loans_context(
        request, "loans",
        form=form, loan=loan, mode=mode, error=error,
        loan_types=LoanType.choices,
        rate_types=RateType.choices,
        frequencies=loan._meta.get_field("payment_frequency").choices,
        borrower_roles=BorrowerRole.choices,
        currencies=Currency.objects.filter(is_active=True),
        base=base_currency(),
        people=people,
        borrower_rows=borrower_rows,
        lender_kind=loan.lender_kind if loan.lender else "",
        lender_name=loan.lender_name,
        lender_tint=loan.lender_tint,
        lender_init=loan.lender_initials,
        bank_accounts=_bank_accounts(),
        expert=expert,
        posting_activities=posting_activities,
        expense_accounts=_expense_accounts(),
    )
    return render(request, "loans/loan_form.html", ctx)


def loan_delete(request, pk):
    loan = get_object_or_404(Loan, pk=pk)
    if request.method == "POST":
        loan.delete()  # soft-delete → Setup → Recently deleted
    return redirect(tenant_url(request, "loans/all/"))


# --- Loan detail ----------------------------------------------------------------------------

def _loan_geo(loan):
    series = loan_value_series(loan)
    points = series["actual"] + series["projected"]
    if len(points) < 2:
        return {}, series
    values = [v for _, v in points]
    dates = [d for d, _ in points]
    geo = loan_chart_points(
        series["actual"], series["projected"],
        min_v=min(values), max_v=max(values), start=min(dates), end=max(dates),
    )
    return geo, series


def loan_detail(request, pk):
    loan = get_object_or_404(
        Loan.objects.select_related(
            "currency", "gl_account", "lender_person", "lender_organization"
        ),
        pk=pk,
    )
    geo, series = _loan_geo(loan)
    projection = payoff_projection(loan)
    ctx = loans_context(
        request, "loans",
        loan=loan, base=base_currency(),
        rows=register(loan),
        borrowers=sorted(loan.borrowers.select_related("person").all(), key=lambda b: b.role_order),
        contributions=contributions_by_borrower(loan),
        interest=interest_by_year(loan),
        rate_changes=list(loan.rate_changes.all()),
        history=loan.history.all()[:60],
        picker_types=PICKER_TYPES,
        bank_accounts=_bank_accounts(),
        expense_accounts=_expense_accounts(),
        loan_geo=geo,
        schedule=projection.get("periods") or [],
        payoff_date=projection.get("payoff_date"),
        remaining_interest=projection.get("remaining_interest"),
        current_rate=loan.current_rate,
    )
    return render(request, "loans/loan_detail.html", ctx)


# --- Transactions ---------------------------------------------------------------------------

def _apply_funding(request, txn):
    src = request.POST.get("funding_source") or Funding.EXTERNAL
    txn.funding_source = src if src in Funding.values else Funding.EXTERNAL
    txn.funding_account = None
    txn.payer_person = None
    txn.payer_organization = None
    if txn.funding_source == Funding.BANK:
        from apps.banking.models import BankAccount

        txn.funding_account = BankAccount.objects.filter(
            pk=request.POST.get("funding_account") or 0
        ).first()
        if txn.funding_account is None:
            txn.funding_source = Funding.CASH
    elif txn.funding_source == Funding.EXTERNAL:
        pid = request.POST.get("payer_person") or ""
        oid = request.POST.get("payer_organization") or ""
        if pid:
            txn.payer_person = Person.objects.filter(pk=pid).first()
        elif oid:
            txn.payer_organization = Organization.objects.filter(pk=oid).first()


def _apply_txn_post(request, txn):
    txn_type = request.POST.get("txn_type", "")
    amount = _decimal(request.POST.get("amount"))
    date = parse_date(request.POST.get("date", "") or "")
    if txn_type not in LoanTxnType.values or date is None:
        return None

    txn.txn_type = txn_type
    txn.date = date
    txn.memo = request.POST.get("memo", "").strip()
    txn.reference = request.POST.get("reference", "").strip()
    txn.cleared = request.POST.get("cleared") in ("on", "1", "true")
    txn.principal = txn.interest = txn.escrow = txn.fees = txn.extra_principal = Decimal("0")
    txn.increase = None
    txn.funding_source = Funding.EXTERNAL
    txn.funding_account = txn.payer_person = txn.payer_organization = None

    if txn_type == LoanTxnType.PAYMENT:
        txn.principal = _decimal(request.POST.get("principal")) or Decimal("0")
        txn.interest = _decimal(request.POST.get("interest")) or Decimal("0")
        txn.escrow = _decimal(request.POST.get("escrow")) or Decimal("0")
        txn.fees = _decimal(request.POST.get("fees")) or Decimal("0")
        txn.extra_principal = _decimal(request.POST.get("extra_principal")) or Decimal("0")
        txn.amount = txn.principal + txn.interest + txn.escrow + txn.fees + txn.extra_principal
        _apply_funding(request, txn)
    elif txn_type == LoanTxnType.EXTRA_PRINCIPAL:
        if amount is None:
            return None
        txn.extra_principal = amount
        txn.amount = amount
        _apply_funding(request, txn)
    elif txn_type in (LoanTxnType.DISBURSEMENT, LoanTxnType.DRAW):
        if amount is None:
            return None
        txn.amount = amount
        _apply_funding(request, txn)
    elif txn_type == LoanTxnType.ADJUSTMENT:
        if amount is None:
            return None
        txn.amount = amount
        txn.increase = request.POST.get("direction", "increase") == "increase"
    else:  # INTEREST / FEE / OPENING
        if amount is None:
            return None
        txn.amount = amount

    if txn.amount is None or txn.amount <= 0:
        return None
    txn.save()
    return txn


def txn_create(request, pk):
    loan = get_object_or_404(Loan, pk=pk)
    if request.method == "POST":
        txn = _apply_txn_post(request, LoanTransaction(loan=loan))
        if txn is not None:
            post_transaction(txn, user=request.user)
            create_matching_leg(txn, user=request.user)
    return redirect(tenant_url(request, f"loans/{pk}/"))


def txn_edit(request, pk, tx):
    loan = get_object_or_404(Loan, pk=pk)
    txn = get_object_or_404(LoanTransaction, pk=tx, loan=loan)
    if request.method == "POST" and _apply_txn_post(request, txn) is not None:
        repost_transaction(txn, user=request.user)
    return redirect(tenant_url(request, f"loans/{pk}/"))


def txn_delete(request, pk, tx):
    loan = get_object_or_404(Loan, pk=pk)
    txn = get_object_or_404(LoanTransaction, pk=tx, loan=loan)
    if request.method == "POST":
        delete_transaction(txn, user=request.user)  # hard-erase the mistake
    return redirect(tenant_url(request, f"loans/{pk}/"))


def rate_add(request, pk):
    loan = get_object_or_404(Loan, pk=pk)
    if request.method == "POST":
        rate = _decimal(request.POST.get("annual_rate"))
        eff = parse_date(request.POST.get("effective_date") or "")
        if rate is not None and eff is not None:
            LoanRateChange.objects.update_or_create(
                loan=loan, effective_date=eff,
                defaults={"annual_rate": rate, "note": request.POST.get("note", "").strip()},
            )
    return redirect(tenant_url(request, f"loans/{pk}/"))


# --- htmx fragments -------------------------------------------------------------------------

def payoff_fragment(request, pk):
    """The what-if projection: re-render the paydown chart + summary for a given extra payment."""
    loan = get_object_or_404(Loan, pk=pk)
    extra = _decimal(request.GET.get("extra")) or Decimal("0")
    base_proj = payoff_projection(loan)
    series = loan_value_series(loan, extra_principal=extra)
    points = series["actual"] + series["projected"]
    geo = {}
    if len(points) >= 2:
        values = [v for _, v in points]
        dates = [d for d, _ in points]
        geo = loan_chart_points(
            series["actual"], series["projected"],
            min_v=min(values), max_v=max(values), start=min(dates), end=max(dates),
        )
    saved = (base_proj.get("remaining_interest") or Decimal("0")) - (
        series.get("remaining_interest") or Decimal("0")
    )
    return render(
        request,
        "loans/partials/payoff.html",
        {
            "loan": loan, "loan_geo": geo, "extra": extra,
            "payoff_date": series.get("payoff_date"),
            "remaining_interest": series.get("remaining_interest"),
            "interest_saved": saved,
            "base": base_currency(),
        },
    )


def payment_split_fragment(request, pk):
    """Suggest the interest/principal split for a payment (the hybrid pre-fill), using the rate in
    effect on the payment date and the loan's live balance."""
    loan = get_object_or_404(Loan, pk=pk)
    on = parse_date(request.GET.get("date") or "") or datetime.date.today()
    total = _decimal(request.GET.get("amount")) or Decimal("0")
    escrow = _decimal(request.GET.get("escrow")) or Decimal("0")
    fees = _decimal(request.GET.get("fees")) or Decimal("0")
    extra = _decimal(request.GET.get("extra")) or Decimal("0")
    pi = total - escrow - fees - extra
    if pi < 0:
        pi = Decimal("0")
    rate = rate_on(loan, on) or Decimal("0")
    split = suggest_split(
        loan.balance, rate / Decimal("100"), pi, frequency=loan.payment_frequency
    )
    return render(
        request,
        "loans/partials/payment_split.html",
        {"split": split, "base": base_currency()},
    )


def lender_search(request):
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
        "loans/partials/lender_search.html",
        {"people": people[:6], "orgs": orgs[:6], "q": q, "role": "lender"},
    )


def borrower_search(request):
    q = request.GET.get("q", "").strip()
    people = Person.objects.all()
    if q:
        people = people.filter(
            Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(preferred_name__icontains=q)
        )
    return render(
        request, "loans/partials/borrower_search.html", {"candidates": people[:8], "q": q}
    )
