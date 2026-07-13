"""Payables views (tenant-scoped, member-accessible).

Thin layer: read POST → model/service → redirect. Templates compose cotton components only. This
first slice is the item/SKU catalog master (mirrors the Investments securities screens); vendors,
bills and payments land in later commits.
"""

from decimal import Decimal, InvalidOperation

from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render

from apps.finance.models import Account, AccountType
from apps.finance.services import base_currency
from apps.payables.forms import ItemForm
from apps.payables.models import Item, ItemSku
from apps.tenants.models import Membership, Role


def tenant_url(request, path=""):
    return f"/t/{request.tenant.schema_name}/{path}"


def _is_owner(request) -> bool:
    return Membership.objects.filter(
        user=request.user, tenant=request.tenant, role=Role.OWNER
    ).exists()


def pay_context(request, active, **extra):
    """Shared context for every Payables page: sidebar counts + which nav item is active."""
    ctx = {
        "active": active,
        "is_owner": _is_owner(request),
        "base": base_currency(),
        "nav_items": Item.objects.count(),
    }
    ctx.update(extra)
    return ctx


def _expense_accounts():
    return Account.objects.filter(type=AccountType.EXPENSE, is_postable=True).order_by("code")


def _asset_accounts():
    return Account.objects.filter(type=AccountType.ASSET, is_postable=True).order_by("code")


def _decimal(raw):
    try:
        return Decimal(str(raw).strip()) if raw not in (None, "") else None
    except (InvalidOperation, ValueError):
        return None


# --- Home ------------------------------------------------------------------------------------

def payables_home(request):
    """The app landing. Until the dashboard lands, redirect to the item catalog."""
    return redirect(tenant_url(request, "payables/items/"))


# --- Items (catalog master) ------------------------------------------------------------------

def item_list(request):
    qs = Item.objects.all()
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(name__icontains=q) | Q(upc__icontains=q) | Q(skus__sku__icontains=q)
        ).distinct()
    kind = request.GET.get("kind", "")
    if kind in Item.Kind.values:
        qs = qs.filter(kind=kind)
    qs = qs.order_by("name")
    page = Paginator(qs, 20).get_page(request.GET.get("page"))
    ctx = pay_context(
        request, "items",
        page=page, items=page.object_list, q=q, kind=kind,
        kinds=Item.Kind.choices, total=Item.objects.count(),
    )
    return render(request, "payables/item_list.html", ctx)


def item_create(request):
    return _item_form(request, Item(), "create")


def item_edit(request, pk):
    return _item_form(request, get_object_or_404(Item, pk=pk), "edit")


def _item_form(request, item, mode):
    form = ItemForm(request.POST or None, instance=item)
    if request.method == "POST" and form.is_valid():
        item = form.save(commit=False)
        item.default_account = _expense_accounts().filter(
            pk=request.POST.get("default_account") or 0
        ).first()
        item.capitalize_default = request.POST.get("capitalize_default") == "on"
        item.asset_account = _asset_accounts().filter(
            pk=request.POST.get("asset_account") or 0
        ).first()
        item.save()
        return redirect(tenant_url(request, f"payables/items/{item.pk}/"))
    ctx = pay_context(
        request, "items",
        form=form, item=item, mode=mode,
        kinds=Item.Kind.choices,
        expense_accounts=_expense_accounts(),
        asset_accounts=_asset_accounts(),
    )
    return render(request, "payables/item_form.html", ctx)


def item_detail(request, pk):
    item = get_object_or_404(Item, pk=pk)
    ctx = pay_context(request, "items", item=item, skus=item.skus.all())
    return render(request, "payables/item_detail.html", ctx)


def item_delete(request, pk):
    item = get_object_or_404(Item, pk=pk)
    if request.method == "POST":
        item.delete()  # soft-delete (Recently deleted can restore)
    return redirect(tenant_url(request, "payables/items/"))


# --- SKUs (per-store) ------------------------------------------------------------------------

def sku_add(request, pk):
    item = get_object_or_404(Item, pk=pk)
    if request.method == "POST":
        sku = request.POST.get("sku", "").strip()
        if sku:
            ItemSku.objects.create(
                item=item,
                store_name=request.POST.get("store_name", "").strip(),
                sku=sku,
                last_price=_decimal(request.POST.get("last_price")),
                note=request.POST.get("note", "").strip(),
            )
    return redirect(tenant_url(request, f"payables/items/{pk}/"))


def sku_edit(request, pk, sku_id):
    item = get_object_or_404(Item, pk=pk)
    row = get_object_or_404(ItemSku, pk=sku_id, item=item)
    if request.method == "POST":
        row.store_name = request.POST.get("store_name", "").strip()
        row.sku = request.POST.get("sku", "").strip() or row.sku
        row.last_price = _decimal(request.POST.get("last_price"))
        row.note = request.POST.get("note", "").strip()
        row.save()
    return redirect(tenant_url(request, f"payables/items/{pk}/"))


def sku_delete(request, pk, sku_id):
    item = get_object_or_404(Item, pk=pk)
    if request.method == "POST":
        ItemSku.objects.filter(pk=sku_id, item=item).delete()
    return redirect(tenant_url(request, f"payables/items/{pk}/"))


# --- htmx fragments --------------------------------------------------------------------------

def item_search(request):
    """htmx: items matching the query (name / UPC / SKU) — to pick an item on a bill line."""
    q = request.GET.get("q", "").strip()
    qs = Item.objects.filter(is_active=True)
    if q:
        qs = qs.filter(
            Q(name__icontains=q) | Q(upc__icontains=q) | Q(skus__sku__icontains=q)
        ).distinct()
    return render(request, "payables/partials/item_search.html", {"items": qs[:8], "q": q})
