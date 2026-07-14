"""Cards views (tenant-scoped, member-accessible). Mirrors the Banking idiom: a dashboard, a credit-
card list + detail (register / holders / history tabs) with popup forms, and a debit-card registry
(list + detail + form). Credit-card money movements post to the ledger through apps.cards.services;
debit cards have no ledger. This layer only reads POST, calls the service, and redirects."""

import datetime
from decimal import Decimal, InvalidOperation

from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_date

from apps.banking.models import BankAccount
from apps.cards.models import (
    CardNetwork,
    CardTxnType,
    CreditCard,
    CreditCardHolder,
    CreditCardTransaction,
    DebitCard,
)
from apps.cards.services import (
    POSTING_ACTIVITIES,
    create_matching_leg,
    dashboard_stats,
    ensure_gl_account,
    post_transaction,
    register,
    repost_transaction,
    sync_holder_p2o,
    unpost_transaction,
)
from apps.contacts.models import Address, Person
from apps.finance.models import Account, Currency
from apps.finance.models import AccountType as GLType
from apps.finance.services import (
    base_currency,
    is_expert_mode,
    posting_map_for,
    set_posting_map,
)
from apps.organizations.models import Branch, Organization
from apps.relationships.services import parse_partial_dates
from apps.setup.models import Category
from apps.tenants.models import Membership, Role

CARD_SORTS = {
    "nickname": ("nickname", "id"),
    "-nickname": ("-nickname", "-id"),
    "added": ("created_at", "id"),
    "-added": ("-created_at", "-id"),
}

# Types offered in the credit-card txn picker (opening comes from the card form).
PICKER_TYPES = [
    (CardTxnType.CHARGE, "Charge"),
    (CardTxnType.PAYMENT, "Payment"),
    (CardTxnType.INTEREST, "Interest"),
    (CardTxnType.FEE, "Fee"),
    (CardTxnType.REFUND, "Refund"),
    (CardTxnType.CREDIT, "Statement credit"),
]


def tenant_url(request, path=""):
    return f"/t/{request.tenant.schema_name}/{path}"


def _is_owner(request) -> bool:
    return Membership.objects.filter(
        user=request.user, tenant=request.tenant, role=Role.OWNER
    ).exists()


def card_context(request, active, **extra):
    ctx = {
        "active": active,
        "is_owner": _is_owner(request),
        "nav_credit": CreditCard.objects.count(),
        "nav_debit": DebitCard.objects.count(),
    }
    ctx.update(extra)
    return ctx


def _issuers():
    """Organizations tagged with the system 'Bank' category (card issuers reuse that seam)."""
    return Organization.objects.filter(
        categories__kind="ORG", categories__name="Bank"
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


def _int(raw):
    try:
        return int((raw or "").strip())
    except (ValueError, TypeError):
        return None


# --- Dashboard ------------------------------------------------------------------------------

def dashboard(request):
    stats = dashboard_stats()
    recent = list(
        CreditCardTransaction.objects.select_related("card").order_by("-date", "-id")[:8]
    )
    ctx = card_context(request, "dashboard", base=base_currency(), recent=recent, **stats)
    return render(request, "cards/dashboard.html", ctx)


# --- Credit cards: list ---------------------------------------------------------------------

def credit_list(request):
    qs = CreditCard.objects.select_related("issuer", "currency").prefetch_related("holders__person")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(nickname__icontains=q) | Q(number__icontains=q) | Q(issuer__name__icontains=q)
        ).distinct()
    sort = request.GET.get("sort", "nickname")
    if sort not in CARD_SORTS:
        sort = "nickname"
    qs = qs.order_by(*CARD_SORTS[sort])
    total = CreditCard.objects.count()
    page = Paginator(qs, 12).get_page(request.GET.get("page"))
    ctx = card_context(
        request, "credit",
        page=page, cards=page.object_list, q=q, sort=sort,
        sort_name_next="-nickname" if sort == "nickname" else "nickname",
        sort_added_next="-added" if sort == "added" else "added",
        total=total, base=base_currency(),
    )
    return render(request, "cards/credit_list.html", ctx)


# --- Credit cards: create / edit / delete ---------------------------------------------------

def _save_holders(request, card):
    ids = request.POST.getlist("holders")
    primary = request.POST.get("primary_holder", "")
    card.holders.all().delete()
    for person in Person.objects.filter(pk__in=ids):
        CreditCardHolder.objects.create(
            card=card, person=person, is_primary=(str(person.pk) == primary)
        )


def _maybe_opening_balance(request, card):
    amount = _decimal(request.POST.get("opening_balance"))
    if amount is None or amount <= 0:
        return
    if card.transactions.filter(txn_type=CardTxnType.OPENING).exists():
        return
    on = parse_date(request.POST.get("opening_date", "") or "") or datetime.date.today()
    txn = CreditCardTransaction.objects.create(
        card=card, txn_type=CardTxnType.OPENING, date=on, amount=amount
    )
    post_transaction(txn, user=request.user)


def _resolve_issuer(request, new_issuer_name, selected_issuer):
    """(issuer). Creates a Bank-category Organization (+ optional branch/city) from the inline
    'add issuer' fields when given; else uses the selected issuer."""
    if not new_issuer_name:
        return selected_issuer
    issuer = Organization.objects.create(name=new_issuer_name)
    issuer.categories.add(Category.objects.get(kind=Category.Kind.ORG, name="Bank"))
    branch = None
    new_branch_name = request.POST.get("new_issuer_branch", "").strip()
    if new_branch_name:
        branch = Branch.objects.create(organization=issuer, name=new_branch_name, is_primary=True)
    city = request.POST.get("new_issuer_city", "").strip()
    if city:
        Address.objects.create(**({"branch": branch} if branch else {"organization": issuer}),
                               city=city, is_primary=True)
    return issuer


def _expert_gl_choice(request):
    gl_mode = request.POST.get("gl_mode", "auto")
    if gl_mode == "parent":
        parent = Account.objects.filter(
            pk=request.POST.get("gl_parent") or 0, is_postable=False, type=GLType.LIABILITY
        ).first()
        return parent, None
    if gl_mode == "existing":
        existing = Account.objects.filter(
            pk=request.POST.get("gl_existing") or 0, is_postable=True, type=GLType.LIABILITY,
            credit_card__isnull=True,
        ).first()
        return None, existing
    return None, None


def _save_posting_maps(request, card):
    for act in POSTING_ACTIVITIES:
        acct_id = request.POST.get(f"map_{act['key']}") or None
        chosen = (
            Account.objects.filter(pk=acct_id, is_postable=True).first() if acct_id else None
        )
        set_posting_map(card, act["key"], chosen)


def credit_create(request):
    return _credit_form(request, CreditCard(), "create")


def credit_edit(request, pk):
    return _credit_form(request, get_object_or_404(CreditCard, pk=pk), "edit")


def _credit_form(request, card, mode):
    expert = is_expert_mode()
    error = ""
    if request.method == "POST":
        new_issuer_name = request.POST.get("new_issuer_name", "").strip()
        selected_issuer = Organization.objects.filter(pk=request.POST.get("issuer") or 0).first()
        have_issuer = bool(new_issuer_name or selected_issuer)
        nickname = request.POST.get("nickname", "").strip()
        network = request.POST.get("network") or CardNetwork.OTHER
        currency = (
            Currency.objects.filter(code=request.POST.get("currency") or "").first()
            or base_currency()
        )
        if nickname and have_issuer and network in CardNetwork.values:
            card.issuer = _resolve_issuer(request, new_issuer_name, selected_issuer)
            card.nickname = nickname
            card.network = network
            card.number = request.POST.get("number", "").strip()
            card.currency = currency
            card.credit_limit = _decimal(request.POST.get("credit_limit"))
            card.statement_day = _int(request.POST.get("statement_day"))
            card.due_day = _int(request.POST.get("due_day"))
            card.apr = _decimal(request.POST.get("apr"))
            card.is_active = request.POST.get("is_active") in ("on", "1", "true")
            card.notes = request.POST.get("notes", "").strip()
            for field, value in parse_partial_dates(request.POST, "opened", "closed").items():
                setattr(card, field, value)
            card.save()
            parent = existing = None
            if expert and mode == "create":
                parent, existing = _expert_gl_choice(request)
            ensure_gl_account(card, parent=parent, existing=existing)
            if expert:
                _save_posting_maps(request, card)
            _save_holders(request, card)
            sync_holder_p2o(card)
            _maybe_opening_balance(request, card)
            return redirect(tenant_url(request, f"cards/credit/{card.pk}/"))
        error = "A nickname and an issuer are required."

    people = Person.objects.filter(is_household_member=True)
    household_ids = set(people.values_list("pk", flat=True))
    current_holders = list(card.holders.select_related("person").all()) if card.pk else []
    selected_holders = {str(h.person_id): h.is_primary for h in current_holders}
    holder_extras = [
        {"id": h.person_id, "name": h.person.display_name,
         "tint": h.person.avatar_tint, "initials": h.person.initials}
        for h in current_holders
        if h.person_id not in household_ids
    ]
    primary_holder = next((str(h.person_id) for h in current_holders if h.is_primary), "")
    pmap = posting_map_for(card) if card.pk else {}
    posting_activities = [
        {**act, "current": pmap.get(act["key"], "")} for act in POSTING_ACTIVITIES
    ]
    ctx = card_context(
        request, "credit",
        card=card, mode=mode, error=error,
        issuers=_issuers(),
        currencies=Currency.objects.filter(is_active=True),
        base=base_currency(),
        networks=CardNetwork.choices,
        people=people,
        selected_holders=selected_holders,
        holder_extras=holder_extras,
        primary_holder=primary_holder,
        expert=expert,
        posting_activities=posting_activities,
        income_accounts=_income_accounts(),
        expense_accounts=_expense_accounts(),
        liability_headers=Account.objects.filter(
            is_postable=False, type=GLType.LIABILITY
        ).order_by("code"),
        adoptable_accounts=Account.objects.filter(
            is_postable=True, type=GLType.LIABILITY, is_system=False, credit_card__isnull=True
        ).order_by("code"),
    )
    return render(request, "cards/credit_form.html", ctx)


def credit_delete(request, pk):
    card = get_object_or_404(CreditCard, pk=pk)
    if request.method == "POST":
        card.delete()  # soft-delete
    return redirect(tenant_url(request, "cards/credit/"))


# --- Credit cards: detail + register --------------------------------------------------------

def credit_detail(request, pk):
    card = get_object_or_404(
        CreditCard.objects.select_related("issuer", "currency", "gl_account"), pk=pk
    )
    reg = register(card, page=request.GET.get("page") or 1)
    ctx = card_context(
        request, "credit",
        card=card,
        register=reg,
        rows=reg["rows"],
        holders=list(card.holders.select_related("person").all()),
        history=card.history.all()[:60],
        base=base_currency(),
        picker_types=PICKER_TYPES,
        income_accounts=_income_accounts(),
        expense_accounts=_expense_accounts(),
        bank_accounts=BankAccount.objects.select_related("bank").all(),
    )
    return render(request, "cards/credit_detail.html", ctx)


# --- Credit-card transactions ----------------------------------------------------------------

def _apply_txn_post(request, txn):
    txn_type = request.POST.get("txn_type", "")
    amount = _decimal(request.POST.get("amount"))
    date = parse_date(request.POST.get("date", "") or "")
    if txn_type not in CardTxnType.values or amount is None or amount <= 0 or date is None:
        return None
    txn.txn_type = txn_type
    txn.date = date
    txn.amount = amount
    txn.memo = request.POST.get("memo", "").strip()
    txn.reference = request.POST.get("reference", "").strip()
    txn.cleared = request.POST.get("cleared") in ("on", "1", "true")

    txn.category_account = None
    if txn_type in (CardTxnType.CHARGE, CardTxnType.REFUND):
        txn.category_account = Account.objects.filter(
            pk=request.POST.get("category_account") or 0, is_postable=True
        ).first()

    txn.counter_account = None
    txn.counter_external = ""
    if txn_type == CardTxnType.PAYMENT:
        txn.counter_account = BankAccount.objects.filter(
            pk=request.POST.get("counter_account") or 0
        ).first()
        txn.counter_external = request.POST.get("counter_external", "").strip()

    txn.payee_person = None
    txn.payee_organization = None
    if txn_type in (CardTxnType.CHARGE, CardTxnType.REFUND):
        pid = request.POST.get("payee_person") or ""
        oid = request.POST.get("payee_organization") or ""
        if pid:
            txn.payee_person = Person.objects.filter(pk=pid).first()
        elif oid:
            txn.payee_organization = Organization.objects.filter(pk=oid).first()

    txn.save()
    return txn


def txn_create(request, pk):
    card = get_object_or_404(CreditCard, pk=pk)
    if request.method == "POST":
        txn = _apply_txn_post(request, CreditCardTransaction(card=card))
        if txn is not None:
            post_transaction(txn, user=request.user)
            if (
                txn.txn_type == CardTxnType.PAYMENT
                and txn.counter_account_id
                and request.POST.get("auto_match")
            ):
                create_matching_leg(txn, user=request.user)
    return redirect(tenant_url(request, f"cards/credit/{pk}/"))


def txn_edit(request, pk, tx):
    card = get_object_or_404(CreditCard, pk=pk)
    txn = get_object_or_404(CreditCardTransaction, pk=tx, card=card)
    if request.method == "POST" and _apply_txn_post(request, txn) is not None:
        repost_transaction(txn, user=request.user)
    return redirect(tenant_url(request, f"cards/credit/{pk}/"))


def txn_delete(request, pk, tx):
    card = get_object_or_404(CreditCard, pk=pk)
    txn = get_object_or_404(CreditCardTransaction, pk=tx, card=card)
    if request.method == "POST":
        unpost_transaction(txn, user=request.user)
        txn.delete()
    return redirect(tenant_url(request, f"cards/credit/{pk}/"))


def txn_toggle_cleared(request, pk, tx):
    card = get_object_or_404(CreditCard, pk=pk)
    txn = get_object_or_404(CreditCardTransaction, pk=tx, card=card)
    if request.method == "POST":
        txn.cleared = not txn.cleared
        txn.save(update_fields=["cleared", "updated_at"])
    return redirect(tenant_url(request, f"cards/credit/{pk}/"))


# --- Debit cards: registry ------------------------------------------------------------------

def debit_list(request):
    qs = DebitCard.objects.select_related("bank_account__bank", "holder")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(nickname__icontains=q) | Q(number__icontains=q)
            | Q(bank_account__nickname__icontains=q)
        ).distinct()
    total = DebitCard.objects.count()
    page = Paginator(qs.order_by("nickname", "id"), 12).get_page(request.GET.get("page"))
    ctx = card_context(
        request, "debit", page=page, cards=page.object_list, q=q, total=total,
        base=base_currency(),
    )
    return render(request, "cards/debit_list.html", ctx)


def debit_create(request):
    return _debit_form(request, DebitCard(), "create")


def debit_edit(request, pk):
    return _debit_form(request, get_object_or_404(DebitCard, pk=pk), "edit")


def _debit_form(request, card, mode):
    error = ""
    if request.method == "POST":
        bank_account = BankAccount.objects.filter(pk=request.POST.get("bank_account") or 0).first()
        nickname = request.POST.get("nickname", "").strip()
        network = request.POST.get("network") or CardNetwork.OTHER
        if bank_account and nickname and network in CardNetwork.values:
            card.bank_account = bank_account
            card.nickname = nickname
            card.network = network
            card.number = request.POST.get("number", "").strip()
            card.holder = Person.objects.filter(pk=request.POST.get("holder") or 0).first()
            card.expiry_month = _int(request.POST.get("expiry_month"))
            card.expiry_year = _int(request.POST.get("expiry_year"))
            card.daily_limit = _decimal(request.POST.get("daily_limit"))
            card.is_active = request.POST.get("is_active") in ("on", "1", "true")
            card.notes = request.POST.get("notes", "").strip()
            card.save()
            return redirect(tenant_url(request, f"cards/debit/{card.pk}/"))
        error = "A linked bank account and a nickname are required."

    ctx = card_context(
        request, "debit",
        card=card, mode=mode, error=error,
        bank_accounts=BankAccount.objects.select_related("bank").all(),
        networks=CardNetwork.choices,
        people=Person.objects.filter(is_household_member=True),
    )
    return render(request, "cards/debit_form.html", ctx)


def debit_detail(request, pk):
    card = get_object_or_404(
        DebitCard.objects.select_related("bank_account__bank", "holder"), pk=pk
    )
    # Bank withdrawals tagged with this debit card (reverse of banking's `card` FK; added in C5).
    spend = (
        card.bank_txns.select_related("account").order_by("-date", "-id")
        if hasattr(card, "bank_txns")
        else []
    )
    ctx = card_context(
        request, "debit", card=card, spend=spend, base=base_currency(),
    )
    return render(request, "cards/debit_detail.html", ctx)


def debit_delete(request, pk):
    card = get_object_or_404(DebitCard, pk=pk)
    if request.method == "POST":
        card.delete()
    return redirect(tenant_url(request, "cards/debit/"))


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
        request, "cards/partials/payee_search.html",
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
        request, "cards/partials/holder_search.html", {"candidates": people[:8], "q": q}
    )
