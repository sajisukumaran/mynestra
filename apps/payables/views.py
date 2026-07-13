"""Payables views (tenant-scoped, member-accessible).

Thin layer: read POST → model/service → redirect. Templates compose cotton components only. This
first slice is the item/SKU catalog master (mirrors the Investments securities screens); vendors,
bills and payments land in later commits.
"""

import datetime
from decimal import Decimal, InvalidOperation

from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_date

from apps.banking.models import BankAccount
from apps.cards.models import CreditCard
from apps.contacts.models import Person
from apps.finance.models import Account, AccountType, Currency
from apps.finance.services import base_currency
from apps.organizations.models import Organization
from apps.payables.forms import ItemForm
from apps.payables.models import Bill, BillLine, Item, ItemSku, Payment, PaymentTerm, VendorProfile
from apps.payables.services import (
    aging,
    apply_payment,
    dashboard_stats,
    delete_bill,
    delete_payment,
    due_soon,
    ensure_vendor_profile,
    open_bills_for,
    post_bill,
    repost_bill,
    repost_payment,
    unpost_bill,
    warranty_expiring,
)
from apps.setup.models import Category
from apps.tenants.models import Membership, Role

ZERO = Decimal("0")


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
        "nav_bills": Bill.objects.count(),
        "nav_vendors": VendorProfile.objects.count(),
        "nav_items": Item.objects.count(),
        "nav_payments": Payment.objects.count(),
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

def dashboard(request):
    """Payables landing: payable/overdue/due-soon stats, an aging breakdown, and the overdue,
    due-soon and warranty-expiry feeds."""
    ctx = pay_context(
        request, "dashboard",
        stats=dashboard_stats(),
        due=due_soon(within_days=14),
        aging=aging(),
        warranty=warranty_expiring(within_days=90),
        recent_bills=Bill.objects.select_related(
            "vendor_person", "vendor_organization"
        ).order_by("-bill_date", "-id")[:6],
        today=datetime.date.today(),
    )
    return render(request, "payables/dashboard.html", ctx)


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


# --- Bills (accrual accounts-payable documents) ----------------------------------------------

def _at(lst, i, default=""):
    return lst[i] if i < len(lst) else default


def _account_by_pk(pk):
    return Account.objects.filter(pk=pk or 0, is_postable=True).first()


def _bill_accounts():
    """Postable expense + asset accounts offered per bill line."""
    return Account.objects.filter(
        type__in=[AccountType.EXPENSE, AccountType.ASSET], is_postable=True
    ).order_by("code")


def bill_list(request):
    qs = Bill.objects.select_related("vendor_person", "vendor_organization", "terms")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(vendor_organization__name__icontains=q)
            | Q(vendor_person__first_name__icontains=q)
            | Q(vendor_person__last_name__icontains=q)
            | Q(vendor_ref__icontains=q)
        )
    status = request.GET.get("status", "")
    if status in Bill.Status.values:
        qs = qs.filter(status=status)
    qs = qs.order_by("-bill_date", "-id")
    page = Paginator(qs, 20).get_page(request.GET.get("page"))
    counts = {s: Bill.objects.filter(status=s).count() for s, _ in Bill.Status.choices}
    ctx = pay_context(
        request, "bills",
        page=page, bills=page.object_list, q=q, status=status,
        statuses=Bill.Status.choices, status_counts=counts, total=Bill.objects.count(),
    )
    return render(request, "payables/bill_list.html", ctx)


def _save_bill_lines(request, bill):
    types = request.POST.getlist("line_type")
    items = request.POST.getlist("line_item")
    descs = request.POST.getlist("line_description")
    qtys = request.POST.getlist("line_quantity")
    prices = request.POST.getlist("line_unit_price")
    discounts = request.POST.getlist("line_discount")
    taxes = request.POST.getlist("line_tax")
    accounts = request.POST.getlist("line_account")
    caps = request.POST.getlist("line_capitalize")
    serials = request.POST.getlist("line_asset_serial")
    warranties = request.POST.getlist("line_warranty_end")
    bill.lines.all().delete()  # replace-all: the bill is editable, its lines rewritten each save
    for i, lt in enumerate(types):
        if lt not in BillLine.LineType.values:
            continue
        desc = _at(descs, i).strip()
        qty = _decimal(_at(qtys, i)) or Decimal("1")
        price = _decimal(_at(prices, i)) or ZERO
        if qty == ZERO and price == ZERO and not desc:
            continue  # skip a blank row
        BillLine.objects.create(
            bill=bill, line_type=lt, order=i,
            item=Item.objects.filter(pk=_at(items, i) or 0).first(),
            description=desc, quantity=qty, unit_price=price,
            line_discount=_decimal(_at(discounts, i)) or ZERO,
            line_tax=_decimal(_at(taxes, i)) or ZERO,
            account=_account_by_pk(_at(accounts, i)),
            capitalize=_at(caps, i) == "1",
            asset_serial=_at(serials, i).strip(),
            warranty_end=parse_date(_at(warranties, i) or ""),
        )


def _apply_bill_post(request, bill):
    """Populate a bill + its lines from POST. Returns False if no vendor was resolved (create)."""
    if bill.pk is None:
        person, org = _resolve_vendor(request)
        if not (person or org):
            return False
        if org:
            _ensure_vendor_category(org)
            ensure_vendor_profile(organization=org)
        else:
            ensure_vendor_profile(person=person)
        bill.vendor_person, bill.vendor_organization = person, org

    bill.vendor_ref = request.POST.get("vendor_ref", "").strip()
    bill.bill_date = parse_date(request.POST.get("bill_date") or "") or bill.bill_date
    if bill.bill_date is None:
        bill.bill_date = datetime.date.today()
    bill.terms = PaymentTerm.objects.filter(pk=request.POST.get("terms") or 0).first()
    explicit_due = parse_date(request.POST.get("due_date") or "")
    bill.due_date = explicit_due or (
        bill.terms.due_date_for(bill.bill_date) if bill.terms else None
    )
    bill.currency = Currency.objects.filter(code=request.POST.get("currency") or "").first()
    bill.notes = request.POST.get("notes", "").strip()
    bill.store_name = request.POST.get("store_name", "").strip()
    bill.order_number = request.POST.get("order_number", "").strip()
    bill.order_date = parse_date(request.POST.get("order_date") or "")
    bill.tracking_number = request.POST.get("tracking_number", "").strip()
    bill.carrier = request.POST.get("carrier", "").strip()
    bill.ship_date = parse_date(request.POST.get("ship_date") or "")
    bill.delivery_date = parse_date(request.POST.get("delivery_date") or "")
    bill.save()
    _save_bill_lines(request, bill)
    return True


def bill_create(request):
    bill = Bill()
    if request.method == "POST" and _apply_bill_post(request, bill):
        post_bill(bill, user=request.user)
        return redirect(tenant_url(request, f"payables/bills/{bill.pk}/"))
    return _render_bill_form(request, bill, "create")


def bill_edit(request, pk):
    bill = get_object_or_404(Bill, pk=pk)
    if bill.is_locked:
        return HttpResponseForbidden("This bill is managed by another module; read-only here.")
    if request.method == "POST" and _apply_bill_post(request, bill):
        repost_bill(bill, user=request.user)
        return redirect(tenant_url(request, f"payables/bills/{bill.pk}/"))
    return _render_bill_form(request, bill, "edit")


def _render_bill_form(request, bill, mode):
    lines_data = [
        {
            "type": li.line_type, "item": str(li.item_id or ""), "description": li.description,
            "qty": str(li.quantity), "price": str(li.unit_price),
            "discount": str(li.line_discount), "tax": str(li.line_tax),
            "account": str(li.account_id or ""), "capitalize": li.capitalize,
            "serial": li.asset_serial,
            "warranty": li.warranty_end.isoformat() if li.warranty_end else "",
        }
        for li in bill.lines.all()
    ] if bill.pk else []
    ctx = pay_context(
        request, "bills",
        bill=bill, mode=mode, lines_data=lines_data,
        terms=PaymentTerm.objects.filter(is_active=True),
        currencies=Currency.objects.filter(is_active=True),
        accounts=_bill_accounts(),
        line_types=BillLine.LineType.choices,
    )
    return render(request, "payables/bill_form.html", ctx)


def bill_detail(request, pk):
    bill = get_object_or_404(
        Bill.objects.select_related("vendor_person", "vendor_organization", "terms"), pk=pk
    )
    ctx = pay_context(
        request, "bills",
        bill=bill, lines=bill.lines.all(), history=bill.history.all()[:40],
    )
    return render(request, "payables/bill_detail.html", ctx)


def bill_void(request, pk):
    bill = get_object_or_404(Bill, pk=pk)
    if request.method == "POST" and not bill.is_locked and bill.status != Bill.Status.VOID:
        unpost_bill(bill, user=request.user)
        bill.status = Bill.Status.VOID
        bill.save(update_fields=["status", "updated_at"])
    return redirect(tenant_url(request, f"payables/bills/{pk}/"))


def bill_delete(request, pk):
    """Erase a mistaken bill: hard-remove its GL entry + the record. Refused when it's locked,
    already void (a kept record), or has any payment allocated (delete the payment first)."""
    bill = get_object_or_404(Bill, pk=pk)
    deletable = (
        not bill.is_locked
        and bill.status != Bill.Status.VOID
        and not bill.allocations.exists()
    )
    if request.method == "POST" and deletable:
        delete_bill(bill, user=request.user)
        bill.hard_delete()
        return redirect(tenant_url(request, "payables/bills/"))
    return redirect(tenant_url(request, f"payables/bills/{pk}/"))


# --- Payments (funding-integrated; allocate across one vendor's bills) ------------------------

def _payment_vendor(request, source):
    """Resolve the payment's vendor party from a bill (?bill=) or explicit (?vendor_kind/id)."""
    getter = request.GET if source == "get" else request.POST
    if source == "get" and getter.get("bill"):
        bill = Bill.objects.filter(pk=getter.get("bill")).first()
        if bill:
            return bill.vendor_person, bill.vendor_organization
    kind = getter.get("vendor_kind")
    vid = getter.get("vendor_id") or 0
    if kind == "person":
        return Person.objects.filter(pk=vid).first(), None
    if kind == "organization":
        return None, Organization.objects.filter(pk=vid).first()
    return None, None


def _payment_bills(person, org, payment=None):
    """The vendor's open bills, plus (on edit) any bill this payment currently allocates to (which
    may now be fully paid). Returns `(bills, existing)` where `existing` maps bill pk → the amount
    this payment already put on it — the room freed if the payment is re-posted."""
    existing = {}
    if payment is not None:
        existing = {a.bill_id: a.amount for a in payment.allocations.all()}
    bills = list(open_bills_for(person=person, organization=org))
    open_ids = {b.pk for b in bills}
    extra_ids = [bid for bid in existing if bid not in open_ids]
    if extra_ids:
        bills += list(Bill.objects.filter(pk__in=extra_ids))
    return bills, existing


def _render_payment_form(request, payment, person, org):
    party = person or org
    bills, existing = _payment_bills(person, org, payment)
    is_edit = payment is not None
    focus = request.GET.get("bill", "")
    bill_rows = []
    for b in bills:
        room = b.balance_due + existing.get(b.pk, ZERO)
        if is_edit:
            prefill = existing.get(b.pk, ZERO)
        else:
            prefill = room if (not focus or str(b.pk) == focus) else ZERO
        bill_rows.append({"bill": b, "room": room, "prefill": prefill})
    ctx = pay_context(
        request, "payments",
        mode="edit" if is_edit else "create",
        payment=payment,
        funding_init=payment.funding_kind if is_edit else Payment.Funding.CASH,
        vendor_kind="person" if person else "organization",
        vendor_id=party.pk,
        vendor_name=getattr(party, "display_name", None) or getattr(party, "name", str(party)),
        bill_rows=bill_rows,
        bank_accounts=BankAccount.objects.all(),
        credit_cards=CreditCard.objects.all(),
        cash_accounts=Account.objects.filter(
            type=AccountType.ASSET, is_postable=True
        ).order_by("code"),
        today=datetime.date.today(),
    )
    return render(request, "payables/payment_form.html", ctx)


def payment_create(request):
    if request.method == "POST":
        return _save_payment(request)
    person, org = _payment_vendor(request, "get")
    if not (person or org):
        return redirect(tenant_url(request, "payables/bills/"))
    return _render_payment_form(request, None, person, org)


def payment_edit(request, pk):
    payment = get_object_or_404(Payment, pk=pk)
    if request.method == "POST":
        return _save_payment(request, payment)
    person, org = payment.vendor_person, payment.vendor_organization
    return _render_payment_form(request, payment, person, org)


def _save_payment(request, payment=None):
    is_edit = payment is not None
    if is_edit:
        person, org = payment.vendor_person, payment.vendor_organization
    else:
        person, org = _payment_vendor(request, "post")
        if not (person or org):
            return redirect(tenant_url(request, "payables/bills/"))
    bills, existing = _payment_bills(person, org, payment)
    allocations = []
    for bill in bills:
        amt = _decimal(request.POST.get(f"alloc_{bill.pk}"))
        if amt and amt > ZERO:
            room = bill.balance_due + existing.get(bill.pk, ZERO)
            allocations.append((bill, min(amt, room)))
    if not allocations:
        if is_edit:
            return redirect(tenant_url(request, f"payables/payments/{payment.pk}/edit/"))
        kind = "person" if person else "organization"
        return redirect(
            tenant_url(request, "payables/payments/new/")
            + f"?vendor_kind={kind}&vendor_id={(person or org).pk}"
        )
    total = sum((a for _, a in allocations), ZERO)
    funding_kind = request.POST.get("funding_kind", Payment.Funding.CASH)
    if not is_edit:
        payment = Payment(vendor_person=person, vendor_organization=org)
    payment.date = parse_date(request.POST.get("date") or "") or datetime.date.today()
    payment.amount = total
    payment.funding_kind = funding_kind
    payment.reference = request.POST.get("reference", "").strip()
    payment.notes = request.POST.get("notes", "").strip()
    payment.bank_account = payment.credit_card = payment.cash_account = None
    if funding_kind == Payment.Funding.BANK:
        payment.bank_account = BankAccount.objects.filter(
            pk=request.POST.get("bank_account") or 0
        ).first()
    elif funding_kind == Payment.Funding.CARD:
        payment.credit_card = CreditCard.objects.filter(
            pk=request.POST.get("credit_card") or 0
        ).first()
    else:
        payment.cash_account = _account_by_pk(request.POST.get("cash_account"))
    if is_edit:
        repost_payment(payment, allocations, user=request.user)
    else:
        payment.save()
        apply_payment(payment, allocations, user=request.user)
    return redirect(tenant_url(request, "payables/payments/"))


def payment_list(request):
    payments = Payment.objects.select_related("vendor_person", "vendor_organization").order_by(
        "-date", "-id"
    )
    page = Paginator(payments, 20).get_page(request.GET.get("page"))
    ctx = pay_context(
        request, "payments",
        page=page, payments=page.object_list, total=Payment.objects.count(),
    )
    return render(request, "payables/payment_list.html", ctx)


def payment_delete(request, pk):
    """Erase a mistaken payment: hard-remove its funding transaction / cash entry + the record;
    the bills it settled reopen."""
    payment = get_object_or_404(Payment, pk=pk)
    if request.method == "POST":
        delete_payment(payment, user=request.user)
        payment.hard_delete()
    return redirect(tenant_url(request, "payables/payments/"))


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
