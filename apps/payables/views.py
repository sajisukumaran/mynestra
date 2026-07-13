"""Payables views (tenant-scoped, member-accessible).

Thin layer: read POST → model/service → redirect. Templates compose cotton components only. This
first slice is the item/SKU catalog master (mirrors the Investments securities screens); vendors,
bills and payments land in later commits.
"""

from decimal import Decimal, InvalidOperation

from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render

from apps.contacts.models import Person
from apps.finance.models import Account, AccountType, Currency
from apps.finance.services import base_currency
from apps.organizations.models import Organization
from apps.payables.forms import ItemForm
from apps.payables.models import Item, ItemSku, PaymentTerm, VendorProfile
from apps.payables.services import ensure_vendor_profile
from apps.setup.models import Category
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
        "nav_vendors": VendorProfile.objects.count(),
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


# --- Vendors (a Person or Organization you owe) ----------------------------------------------

def _ensure_vendor_category(org):
    cat = Category.objects.filter(kind=Category.Kind.ORG, name="Vendor").first()
    if cat:
        org.categories.add(cat)


def _resolve_vendor(request):
    """The bill/vendor party from POST: an inline-created Org, or a picked Person/Org. Returns
    (person, organization) with exactly one set, or (None, None) if unresolved."""
    new_name = request.POST.get("new_vendor_name", "").strip()
    if new_name:
        org = Organization.objects.create(name=new_name)
        _ensure_vendor_category(org)
        return None, org
    kind = request.POST.get("party_kind", "")
    pid = request.POST.get("party_id") or 0
    if kind == "person":
        return Person.objects.filter(pk=pid).first(), None
    if kind == "organization":
        return None, Organization.objects.filter(pk=pid).first()
    return None, None


def _apply_vendor_defaults(request, profile):
    profile.default_terms = PaymentTerm.objects.filter(
        pk=request.POST.get("default_terms") or 0
    ).first()
    profile.default_expense_account = _expense_accounts().filter(
        pk=request.POST.get("default_expense_account") or 0
    ).first()
    profile.currency = Currency.objects.filter(code=request.POST.get("currency") or "").first()
    profile.account_number = request.POST.get("account_number", "").strip()
    profile.notes = request.POST.get("notes", "").strip()
    profile.is_active = request.POST.get("is_active") == "on"
    profile.save()


def vendor_list(request):
    qs = VendorProfile.objects.select_related("person", "organization")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(organization__name__icontains=q)
            | Q(organization__display_name__icontains=q)
            | Q(person__first_name__icontains=q)
            | Q(person__last_name__icontains=q)
            | Q(person__preferred_name__icontains=q)
        )
    ptype = request.GET.get("type", "")
    if ptype == "person":
        qs = qs.filter(person__isnull=False)
    elif ptype == "organization":
        qs = qs.filter(organization__isnull=False)
    qs = qs.order_by("-id")
    page = Paginator(qs, 20).get_page(request.GET.get("page"))
    ctx = pay_context(
        request, "vendors",
        page=page, vendors=page.object_list, q=q, ptype=ptype,
        total=VendorProfile.objects.count(),
    )
    return render(request, "payables/vendor_list.html", ctx)


def vendor_create(request):
    if request.method == "POST":
        person, org = _resolve_vendor(request)
        if person or org:
            if org:
                _ensure_vendor_category(org)
            profile = ensure_vendor_profile(person=person, organization=org)
            _apply_vendor_defaults(request, profile)
            return redirect(tenant_url(request, f"payables/vendors/{profile.pk}/"))
        return _render_vendor_form(request, VendorProfile(), "create", error="Choose a vendor.")
    return _render_vendor_form(request, VendorProfile(), "create")


def vendor_edit(request, pk):
    profile = get_object_or_404(VendorProfile, pk=pk)
    if request.method == "POST":
        _apply_vendor_defaults(request, profile)
        return redirect(tenant_url(request, f"payables/vendors/{profile.pk}/"))
    return _render_vendor_form(request, profile, "edit")


def _render_vendor_form(request, profile, mode, error=""):
    ctx = pay_context(
        request, "vendors",
        vendor=profile, mode=mode, error=error,
        terms=PaymentTerm.objects.filter(is_active=True),
        expense_accounts=_expense_accounts(),
        currencies=Currency.objects.filter(is_active=True),
    )
    return render(request, "payables/vendor_form.html", ctx)


def vendor_detail(request, pk):
    profile = get_object_or_404(VendorProfile, pk=pk)
    ctx = pay_context(request, "vendors", vendor=profile)
    return render(request, "payables/vendor_detail.html", ctx)


def vendor_delete(request, pk):
    profile = get_object_or_404(VendorProfile, pk=pk)
    if request.method == "POST":
        profile.delete()  # soft-delete (the underlying Person/Org is untouched)
    return redirect(tenant_url(request, "payables/vendors/"))


# --- htmx fragments --------------------------------------------------------------------------

def vendor_search(request):
    """htmx: People + Organizations matching the query — candidates to pick as a vendor."""
    q = request.GET.get("q", "").strip()
    people = Person.objects.none()
    orgs = Organization.objects.none()
    if q:
        people = Person.objects.filter(
            Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(preferred_name__icontains=q)
        )[:6]
        orgs = Organization.objects.filter(
            Q(name__icontains=q) | Q(display_name__icontains=q)
        )[:6]
    return render(
        request, "payables/partials/vendor_search.html", {"people": people, "orgs": orgs, "q": q}
    )


def item_search(request):
    """htmx: items matching the query (name / UPC / SKU) — to pick an item on a bill line."""
    q = request.GET.get("q", "").strip()
    qs = Item.objects.filter(is_active=True)
    if q:
        qs = qs.filter(
            Q(name__icontains=q) | Q(upc__icontains=q) | Q(skus__sku__icontains=q)
        ).distinct()
    return render(request, "payables/partials/item_search.html", {"items": qs[:8], "q": q})
