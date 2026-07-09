"""Finance app views — a read-only Chart of Accounts with computed balances.

This is the only finance screen for now: the ledger and fiscal calendar stay invisible; their
aggregate balances surface here. Balances are computed from posted lines (services layer) and
rolled up the account tree in one pass. Member-accessible (household finances are shared)."""

from decimal import Decimal

from django.db.models import Sum
from django.shortcuts import render

from apps.finance.models import Account, JournalEntry, JournalLine
from apps.finance.services import base_currency
from apps.tenants.models import Membership, Role

ZERO = Decimal("0")


def _is_owner(request) -> bool:
    return Membership.objects.filter(
        user=request.user, tenant=request.tenant, role=Role.OWNER
    ).exists()


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
    }
    return render(request, "finance/chart_of_accounts.html", ctx)
