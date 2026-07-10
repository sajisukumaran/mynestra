"""Finance app views — the Chart of Accounts with computed balances.

The whole Finance surface is Expert-mode only (hidden + route-guarded in Standard, where the GL is
invisible). In Expert mode any member can view the chart; Owners can also edit it (add / edit /
reparent / delete). Balances are computed from posted lines and rolled up the account tree in one
pass."""

from decimal import Decimal

from django.db.models import Sum
from django.shortcuts import get_object_or_404, redirect, render

from apps.accounts.decorators import expert_required, owner_required
from apps.finance.exceptions import COAEditError
from apps.finance.models import Account, AccountType, JournalEntry, JournalLine
from apps.finance.services import (
    base_currency,
    create_account,
    delete_account,
    edit_account,
)
from apps.tenants.models import Membership, Role

ZERO = Decimal("0")


def _is_owner(request) -> bool:
    return Membership.objects.filter(
        user=request.user, tenant=request.tenant, role=Role.OWNER
    ).exists()


def _tenant_url(request, path=""):
    return f"/t/{request.tenant.schema_name}/{path}"


@expert_required
def finance_home(request):
    """Chart of accounts + base-currency balances (headers roll up their subtree)."""
    accounts = list(Account.objects.all())  # ordered by code
    posted_sums = (
        JournalLine.objects.filter(entry__status=JournalEntry.Status.POSTED)
        .values("account_id")
        .annotate(d=Sum("base_debit"), c=Sum("base_credit"))
    )
    own = {row["account_id"]: (row["d"] or ZERO) - (row["c"] or ZERO) for row in posted_sums}

    children: dict = {}
    for account in accounts:
        children.setdefault(account.parent_id, []).append(account)

    rows: list = []

    def walk(account, depth):
        subtotal = own.get(account.pk, ZERO)
        index = len(rows)
        rows.append(None)  # reserve slot; balance filled after children are summed
        for child in children.get(account.pk, []):
            subtotal += walk(child, depth + 1)
        rows[index] = {
            "account": account,
            "depth": depth,
            "indent": depth * 18,
            "balance": subtotal * account.normal_sign,
            "is_header": not account.is_postable,
        }
        return subtotal

    for root in children.get(None, []):
        walk(root, 0)

    ctx = {
        "active": "accounts",
        "is_owner": _is_owner(request),
        "rows": rows,
        "base": base_currency(),
        "has_postings": bool(own),
        # Editor (Owner-only affordances, gated in the template on is_owner):
        "all_accounts": accounts,
        "types": AccountType.choices,
    }
    return render(request, "finance/chart_of_accounts.html", ctx)


# --- Chart-of-Accounts editor (Expert + Owner) ----------------------------------------------

def _account_from_post(request):
    parent = Account.objects.filter(pk=request.POST.get("parent") or 0).first()
    return {
        "code": request.POST.get("code", ""),
        "name": request.POST.get("name", ""),
        "account_type": request.POST.get("type", ""),
        "parent": parent,
        "is_postable": request.POST.get("is_header") not in ("on", "1", "true"),
        "description": request.POST.get("description", "").strip(),
    }


@expert_required
@owner_required
def account_create(request):
    if request.method == "POST":
        data = _account_from_post(request)
        data.pop("is_active", None)
        try:
            create_account(**data)
        except COAEditError as exc:
            return _coa_error(request, exc)
    return redirect(_tenant_url(request, "finance/"))


@expert_required
@owner_required
def account_edit(request, pk):
    account = get_object_or_404(Account, pk=pk)
    if request.method == "POST":
        data = _account_from_post(request)
        data["is_active"] = request.POST.get("is_active") in ("on", "1", "true")
        try:
            edit_account(account, **data)
        except COAEditError as exc:
            return _coa_error(request, exc)
    return redirect(_tenant_url(request, "finance/"))


@expert_required
@owner_required
def account_delete(request, pk):
    account = get_object_or_404(Account, pk=pk)
    if request.method == "POST":
        try:
            delete_account(account)
        except COAEditError as exc:
            return _coa_error(request, exc)
    return redirect(_tenant_url(request, "finance/"))


def _coa_error(request, exc):
    """Bounce back to the chart with an inline error banner (query param; editor is Owner-only)."""
    from urllib.parse import urlencode

    return redirect(_tenant_url(request, "finance/") + "?" + urlencode({"err": str(exc)}))
