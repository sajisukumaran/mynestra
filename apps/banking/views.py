"""Banking views (tenant-scoped, member-accessible). Mirrors the Contacts/Organizations idiom:
a dashboard, an accounts list (search / type filter chips / sort / paginate), an account detail with
a transaction register + holders + history tabs, and popup (c-modal) forms for accounts and
transactions. Every money movement is posted to the ledger through apps.banking.services; this layer
only reads POST, calls the service, and redirects."""

import datetime
from decimal import Decimal, InvalidOperation

from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_date

from apps.banking.forms import BankAccountForm
from apps.banking.models import (
    AccountType,
    BankAccount,
    BankAccountHolder,
    BankTransaction,
    TxnType,
)
from apps.banking.services import (
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

ACCOUNT_SORTS = {
    "nickname": ("nickname", "id"),
    "-nickname": ("-nickname", "-id"),
    "added": ("created_at", "id"),
    "-added": ("-created_at", "-id"),
}

# Types offered in the "add transaction" picker (opening is created via the account form).
PICKER_TYPES = [
    (TxnType.DEPOSIT, "Deposit"),
    (TxnType.WITHDRAWAL, "Withdrawal"),
    (TxnType.INTEREST, "Interest"),
    (TxnType.FEE, "Fee"),
    (TxnType.CHARGE, "Charge"),
    (TxnType.TRANSFER_OUT, "Transfer out"),
    (TxnType.TRANSFER_IN, "Transfer in"),
]


def tenant_url(request, path=""):
    return f"/t/{request.tenant.schema_name}/{path}"


def _is_owner(request) -> bool:
    return Membership.objects.filter(
        user=request.user, tenant=request.tenant, role=Role.OWNER
    ).exists()


def bank_context(request, active, **extra):
    ctx = {
        "active": active,
        "is_owner": _is_owner(request),
        "nav_accounts": BankAccount.objects.count(),
    }
    ctx.update(extra)
    return ctx


def _banks():
    """Organizations tagged with the system 'Bank' category (the seam banking is built on)."""
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


# --- Dashboard ------------------------------------------------------------------------------

def dashboard(request):
    stats = dashboard_stats()
    recent = list(
        BankTransaction.objects.select_related("account").order_by("-date", "-id")[:8]
    )
    ctx = bank_context(request, "dashboard", base=base_currency(), recent=recent, **stats)
    return render(request, "banking/dashboard.html", ctx)


# --- Accounts list --------------------------------------------------------------------------

def account_list(request):
    qs = BankAccount.objects.select_related("bank", "currency").prefetch_related("holders__person")

    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(nickname__icontains=q) | Q(number__icontains=q) | Q(bank__name__icontains=q)
        ).distinct()

    atype = request.GET.get("type", "")
    if atype in AccountType.values:
        qs = qs.filter(account_type=atype)

    sort = request.GET.get("sort", "nickname")
    if sort not in ACCOUNT_SORTS:
        sort = "nickname"
    qs = qs.order_by(*ACCOUNT_SORTS[sort])

    total = BankAccount.objects.count()
    counts = {
        "checking": BankAccount.objects.filter(account_type=AccountType.CHECKING).count(),
        "savings": BankAccount.objects.filter(account_type=AccountType.SAVINGS).count(),
    }
    page = Paginator(qs, 12).get_page(request.GET.get("page"))

    ctx = bank_context(
        request, "accounts",
        page=page, accounts=page.object_list, q=q, type=atype, sort=sort,
        sort_name_next="-nickname" if sort == "nickname" else "nickname",
        sort_added_next="-added" if sort == "added" else "added",
        total=total, counts=counts, base=base_currency(),
    )
    return render(request, "banking/account_list.html", ctx)


# --- Account create / edit / delete ---------------------------------------------------------

def _save_holders(request, account):
    ids = request.POST.getlist("holders")
    primary = request.POST.get("primary_holder", "")
    account.holders.all().delete()
    for person in Person.objects.filter(pk__in=ids):
        BankAccountHolder.objects.create(
            account=account, person=person, is_primary=(str(person.pk) == primary)
        )


def _maybe_opening_balance(request, account):
    """Create the opening-balance transaction on setup (skipped if one already exists)."""
    amount = _decimal(request.POST.get("opening_balance"))
    if amount is None or amount <= 0:
        return
    if account.transactions.filter(txn_type=TxnType.OPENING).exists():
        return
    on = parse_date(request.POST.get("opening_date", "") or "") or datetime.date.today()
    txn = BankTransaction.objects.create(
        account=account, txn_type=TxnType.OPENING, date=on, amount=amount
    )
    post_transaction(txn, user=request.user)


def account_create(request):
    return _account_form(request, BankAccount(), "create")


def account_edit(request, pk):
    return _account_form(request, get_object_or_404(BankAccount, pk=pk), "edit")


def _expert_gl_choice(request):
    """Expert-mode GL-node choice for a NEW bank account: (parent header, existing account)."""
    gl_mode = request.POST.get("gl_mode", "auto")
    if gl_mode == "parent":
        parent = Account.objects.filter(
            pk=request.POST.get("gl_parent") or 0, is_postable=False, type=GLType.ASSET
        ).first()
        return parent, None
    if gl_mode == "existing":
        existing = Account.objects.filter(
            pk=request.POST.get("gl_existing") or 0, is_postable=True, type=GLType.ASSET,
            bank_account__isnull=True,
        ).first()
        return None, existing
    return None, None


def _save_posting_maps(request, account):
    """Persist the Accounting Setup tab's per-activity account overrides (Expert mode)."""
    for act in POSTING_ACTIVITIES:
        acct_id = request.POST.get(f"map_{act['key']}") or None
        chosen = (
            Account.objects.filter(pk=acct_id, is_postable=True).first() if acct_id else None
        )
        set_posting_map(account, act["key"], chosen)


def _account_form(request, account, mode):
    form = BankAccountForm(request.POST or None, instance=account)
    expert = is_expert_mode()
    error = ""
    if request.method == "POST":
        new_bank_name = request.POST.get("new_bank_name", "").strip()
        selected_bank = Organization.objects.filter(pk=request.POST.get("bank") or 0).first()
        have_bank = bool(new_bank_name or selected_bank)
        account_type = request.POST.get("account_type") or AccountType.CHECKING
        currency = (
            Currency.objects.filter(code=request.POST.get("currency") or "").first()
            or base_currency()
        )
        if form.is_valid() and have_bank and account_type in AccountType.values:
            bank, branch = _resolve_bank(request, new_bank_name, selected_bank)
            account = form.save(commit=False)
            account.bank = bank
            account.branch = branch
            account.account_type = account_type
            account.currency = currency
            for field, value in parse_partial_dates(request.POST, "opened", "closed").items():
                setattr(account, field, value)
            account.save()
            # Expert may direct where the account's own ledger node lives (create only).
            parent = existing = None
            if expert and mode == "create":
                parent, existing = _expert_gl_choice(request)
            ensure_gl_account(account, parent=parent, existing=existing)
            if expert:
                _save_posting_maps(request, account)
            _save_holders(request, account)
            sync_holder_p2o(account)
            _maybe_opening_balance(request, account)
            return redirect(tenant_url(request, f"banking/accounts/{account.pk}/"))
        if not have_bank:
            error = "Choose a bank or add a new one."

    people = Person.objects.filter(is_household_member=True)
    household_ids = set(people.values_list("pk", flat=True))
    current_holders = list(account.holders.select_related("person").all()) if account.pk else []
    selected_holders = {str(h.person_id): h.is_primary for h in current_holders}
    # Holders who aren't household members render as removable "extra" chips (added via search).
    holder_extras = [
        {"id": h.person_id, "name": h.person.display_name,
         "tint": h.person.avatar_tint, "initials": h.person.initials}
        for h in current_holders
        if h.person_id not in household_ids
    ]
    primary_holder = next((str(h.person_id) for h in current_holders if h.is_primary), "")
    branches = (
        Branch.objects.filter(organization=account.bank)
        if account.bank_id
        else Branch.objects.none()
    )
    # Expert-mode "Accounting" tab: per-activity account overrides + this account's ledger node.
    pmap = posting_map_for(account) if account.pk else {}
    posting_activities = [
        {**act, "current": pmap.get(act["key"], "")} for act in POSTING_ACTIVITIES
    ]
    ctx = bank_context(
        request, "accounts",
        form=form, account=account, mode=mode, error=error,
        banks=_banks(),
        branches=branches,
        currencies=Currency.objects.filter(is_active=True),
        base=base_currency(),
        account_types=AccountType.choices,
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
            is_postable=True, type=GLType.ASSET, is_system=False, bank_account__isnull=True
        ).order_by("code"),
    )
    return render(request, "banking/account_form.html", ctx)


def _resolve_bank(request, new_bank_name, selected_bank):
    """Return (bank, branch). Creates a Bank-category Organization (+ optional branch/city) from the
    inline 'add a new bank' fields when given; else uses the selected bank + its posted branch."""
    if not new_bank_name:
        branch = (
            Branch.objects.filter(pk=request.POST.get("branch") or 0, organization=selected_bank)
            .first()
            if selected_bank
            else None
        )
        return selected_bank, branch

    bank = Organization.objects.create(name=new_bank_name)
    bank.categories.add(Category.objects.get(kind=Category.Kind.ORG, name="Bank"))
    branch = None
    new_branch_name = request.POST.get("new_branch_name", "").strip()
    if new_branch_name:
        branch = Branch.objects.create(organization=bank, name=new_branch_name, is_primary=True)
    city = request.POST.get("new_bank_city", "").strip()
    if city:
        Address.objects.create(**({"branch": branch} if branch else {"organization": bank}),
                               city=city, is_primary=True)
    return bank, branch


def account_delete(request, pk):
    account = get_object_or_404(BankAccount, pk=pk)
    if request.method == "POST":
        account.delete()  # soft-delete → Setup → Recently deleted
    return redirect(tenant_url(request, "banking/accounts/"))


# --- Account detail (Register / Holders / History) ------------------------------------------

def account_detail(request, pk):
    account = get_object_or_404(
        BankAccount.objects.select_related("bank", "branch", "currency", "gl_account"), pk=pk
    )
    ctx = bank_context(
        request, "accounts",
        account=account,
        rows=register(account),
        holders=list(account.holders.select_related("person").all()),
        history=account.history.all()[:60],
        base=base_currency(),
        picker_types=PICKER_TYPES,
        income_accounts=_income_accounts(),
        expense_accounts=_expense_accounts(),
        cash_account=Account.objects.filter(code="1110").first(),
        other_accounts=BankAccount.objects.exclude(pk=account.pk),
    )
    return render(request, "banking/account_detail.html", ctx)


# --- Transactions ---------------------------------------------------------------------------

def _apply_txn_post(request, txn):
    """Populate a (new or existing) transaction from POST; save + return it, or None if invalid."""
    txn_type = request.POST.get("txn_type", "")
    amount = _decimal(request.POST.get("amount"))
    date = parse_date(request.POST.get("date", "") or "")
    if txn_type not in TxnType.values or amount is None or amount <= 0 or date is None:
        return None

    txn.txn_type = txn_type
    txn.date = date
    txn.amount = amount
    txn.memo = request.POST.get("memo", "").strip()
    txn.reference = request.POST.get("reference", "").strip()
    txn.cleared = request.POST.get("cleared") in ("on", "1", "true")

    txn.category_account = None
    if txn_type in (TxnType.DEPOSIT, TxnType.WITHDRAWAL):
        txn.category_account = Account.objects.filter(
            pk=request.POST.get("category_account") or 0, is_postable=True
        ).first()

    txn.counter_account = None
    txn.counter_external = ""
    if txn_type in (TxnType.TRANSFER_OUT, TxnType.TRANSFER_IN):
        txn.counter_account = (
            BankAccount.objects.filter(pk=request.POST.get("counter_account") or 0)
            .exclude(pk=txn.account_id)
            .first()
        )
        txn.counter_external = request.POST.get("counter_external", "").strip()

    txn.payee_person = None
    txn.payee_organization = None
    if txn_type in (TxnType.DEPOSIT, TxnType.WITHDRAWAL):
        pid = request.POST.get("payee_person") or ""
        oid = request.POST.get("payee_organization") or ""
        if pid:
            txn.payee_person = Person.objects.filter(pk=pid).first()
        elif oid:
            txn.payee_organization = Organization.objects.filter(pk=oid).first()

    txn.save()
    return txn


def txn_create(request, pk):
    account = get_object_or_404(BankAccount, pk=pk)
    if request.method == "POST":
        txn = _apply_txn_post(request, BankTransaction(account=account))
        if txn is not None:
            post_transaction(txn, user=request.user)
            if (
                txn.txn_type in (TxnType.TRANSFER_OUT, TxnType.TRANSFER_IN)
                and txn.counter_account_id
                and request.POST.get("auto_match")
            ):
                create_matching_leg(txn, user=request.user)
    return redirect(tenant_url(request, f"banking/accounts/{pk}/"))


def txn_edit(request, pk, tx):
    account = get_object_or_404(BankAccount, pk=pk)
    txn = get_object_or_404(BankTransaction, pk=tx, account=account)
    if request.method == "POST" and _apply_txn_post(request, txn) is not None:
        repost_transaction(txn, user=request.user)
    return redirect(tenant_url(request, f"banking/accounts/{pk}/"))


def txn_delete(request, pk, tx):
    account = get_object_or_404(BankAccount, pk=pk)
    txn = get_object_or_404(BankTransaction, pk=tx, account=account)
    if request.method == "POST":
        unpost_transaction(txn, user=request.user)
        txn.delete()  # soft-delete
    return redirect(tenant_url(request, f"banking/accounts/{pk}/"))


def txn_toggle_cleared(request, pk, tx):
    account = get_object_or_404(BankAccount, pk=pk)
    txn = get_object_or_404(BankTransaction, pk=tx, account=account)
    if request.method == "POST":
        txn.cleared = not txn.cleared
        txn.save(update_fields=["cleared", "updated_at"])
    return redirect(tenant_url(request, f"banking/accounts/{pk}/"))


# --- htmx fragments -------------------------------------------------------------------------

def payee_search(request):
    """People + organizations for the optional payee picker on deposits/withdrawals."""
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
        "banking/partials/payee_search.html",
        {"people": people[:6], "orgs": orgs[:6], "q": q},
    )


def holder_search(request):
    """People for the account-holder picker's 'add someone else' search (any contact)."""
    q = request.GET.get("q", "").strip()
    people = Person.objects.all()
    if q:
        people = people.filter(
            Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(preferred_name__icontains=q)
        )
    return render(
        request, "banking/partials/holder_search.html", {"candidates": people[:8], "q": q}
    )


def branch_options(request):
    """Branch <option>s for the account form's dependent select (fires when the bank changes)."""
    branches = Branch.objects.filter(organization_id=request.GET.get("bank") or 0)
    return render(request, "banking/partials/branch_options.html", {"branches": branches})
