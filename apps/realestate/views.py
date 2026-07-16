"""Real Estate views (tenant-scoped, member-accessible). Mirrors the Automobile idiom: a dashboard,
a property list (search / type + use chips / sort / paginate), a property detail with cost register,
valuations, people + history tabs, and popup (c-modal) forms. Every money movement goes through
apps.realestate.services (locked payables bills/payments, or a direct disposal entry); this layer
reads POST, calls the service, and redirects."""

import datetime
from decimal import Decimal, InvalidOperation

from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_date

from apps.contacts.models import Person
from apps.finance.models import Account, AccountType, Currency
from apps.finance.services import base_currency
from apps.investments.services import line_chart_points
from apps.organizations.models import Organization
from apps.realestate.forms import PropertyForm
from apps.realestate.models import (
    CostKind,
    DisposalMethod,
    DocumentType,
    Funding,
    OwnerRole,
    OwnershipMode,
    Property,
    PropertyCostEvent,
    PropertyDisposal,
    PropertyDocument,
    PropertyOwner,
    PropertyType,
    PropertyUse,
    PropertyValuation,
)
from apps.realestate.services import (
    appreciation_series,
    dashboard_stats,
    delete_cost_event,
    delete_document,
    ensure_gl_account,
    post_disposal,
    save_cost_event,
    settle_financed_purchase,
)
from apps.tenants.models import Membership, Role

PROPERTY_SORTS = {
    "nickname": ("nickname", "id"),
    "-nickname": ("-nickname", "-id"),
    "added": ("created_at", "id"),
    "-added": ("-created_at", "-id"),
}

# Cost kinds offered in the register's "add cost" picker (purchase comes from the acquisition flow).
COST_PICKER_KINDS = [
    (CostKind.PROPERTY_TAX, "Property tax"),
    (CostKind.MAINTENANCE, "Maintenance / repair"),
    (CostKind.HOA, "HOA / condo fees"),
    (CostKind.UTILITIES, "Utilities"),
    (CostKind.IMPROVEMENT, "Improvement / renovation"),
    (CostKind.CLOSING_COST, "Closing costs"),
    (CostKind.OTHER, "Other"),
]


def tenant_url(request, path=""):
    return f"/t/{request.tenant.schema_name}/{path}"


def _is_owner(request) -> bool:
    return Membership.objects.filter(
        user=request.user, tenant=request.tenant, role=Role.OWNER
    ).exists()


def realestate_context(request, active, **extra):
    ctx = {
        "active": active,
        "is_owner": _is_owner(request),
        "nav_properties": Property.objects.count(),
    }
    ctx.update(extra)
    return ctx


def _decimal(raw):
    try:
        return Decimal((raw or "").strip())
    except (InvalidOperation, TypeError):
        return None


def _int(raw):
    raw = (raw or "").strip()
    return int(raw) if raw.lstrip("-").isdigit() else None


def _bank_accounts():
    from apps.banking.models import BankAccount

    return BankAccount.objects.select_related("bank").all()


def _credit_cards():
    from apps.cards.models import CreditCard

    return CreditCard.objects.all()


def _cash_accounts():
    return Account.objects.filter(type=AccountType.ASSET, is_postable=True).order_by("code")


def _mortgage_loans():
    from apps.loans.models import Loan

    return Loan.objects.filter(is_active=True)


def _household_people():
    return Person.objects.filter(is_household_member=True)


def _resolve_org(request, field):
    """A picked org id or an inline-created org by name (mirrors automobile/payables)."""
    new_name = request.POST.get(f"{field}_new_name", "").strip()
    if new_name:
        return Organization.objects.create(name=new_name)
    oid = request.POST.get(field) or 0
    return Organization.objects.filter(pk=oid).first()


# --- Dashboard ------------------------------------------------------------------------------

def dashboard(request):
    stats = dashboard_stats()
    properties = stats["properties"]
    bar_items = sorted(
        (
            {"label": p.nickname, "value": p.cost, "tint": p.type_tint}
            for p in properties if p.cost > 0
        ),
        key=lambda b: b["value"], reverse=True,
    )
    bars_total = sum((b["value"] for b in bar_items), Decimal("0"))
    recent = list(
        PropertyCostEvent.objects.select_related("property").order_by("-date", "-id")[:8]
    )
    ctx = realestate_context(
        request, "dashboard", base=base_currency(),
        bar_items=bar_items, bar_total=bars_total, recent=recent, **stats,
    )
    return render(request, "realestate/dashboard.html", ctx)


# --- Property list --------------------------------------------------------------------------

def property_list(request):
    qs = Property.objects.select_related("currency", "gl_account", "mortgage_loan")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(nickname__icontains=q) | Q(address_line1__icontains=q)
            | Q(city__icontains=q) | Q(postal_code__icontains=q)
        ).distinct()

    ptype = request.GET.get("type", "")
    if ptype in PropertyType.values:
        qs = qs.filter(property_type=ptype)
    use = request.GET.get("use", "")
    if use in PropertyUse.values:
        qs = qs.filter(use=use)

    sort = request.GET.get("sort", "nickname")
    if sort not in PROPERTY_SORTS:
        sort = "nickname"
    qs = qs.order_by(*PROPERTY_SORTS[sort])

    total = Property.objects.count()
    type_chips = [
        {"val": val, "label": label,
         "count": Property.objects.filter(property_type=val).count()}
        for val, label in PropertyType.choices
    ]
    page = Paginator(qs, 12).get_page(request.GET.get("page"))
    ctx = realestate_context(
        request, "properties",
        page=page, properties=list(page.object_list), q=q, type=ptype, use=use, sort=sort,
        sort_name_next="-nickname" if sort == "nickname" else "nickname",
        total=total, type_chips=type_chips, uses=PropertyUse.choices, base=base_currency(),
    )
    return render(request, "realestate/property_list.html", ctx)


# --- Property create / edit / delete --------------------------------------------------------

def _save_owners(request, property):
    pids = request.POST.getlist("owner_person")
    roles = request.POST.getlist("owner_role")
    property.owners.all().delete()
    seen = set()
    for pid, role in zip(pids, roles, strict=False):
        if not pid or pid in seen:
            continue
        seen.add(pid)
        person = Person.objects.filter(pk=pid).first()
        if person is None:
            continue
        PropertyOwner.objects.create(
            property=property, person=person,
            role=role if role in OwnerRole.values else OwnerRole.OWNER,
        )


def _maybe_acquisition(request, property):
    """On create only: record the purchase via the service layer. A financed property uses
    settle_financed_purchase (down payment + mortgage); an owned-cash property a single
    funded/unfunded purchase bill. Requires a seller org (a bill needs exactly one vendor)."""
    price = _decimal(request.POST.get("purchase_price"))
    if price is None or price <= 0:
        return
    on = parse_date(request.POST.get("purchase_date") or "") or datetime.date.today()
    seller = property.seller_organization
    if seller is None:
        return  # exactly-one vendor required; skip the purchase if no seller chosen
    event = PropertyCostEvent(
        property=property, kind=CostKind.PURCHASE, date=on, amount=price,
        vendor_organization=seller, vendor_person=None,
    )
    event.save()
    if property.is_financed:
        loan = _mortgage_loans().filter(pk=request.POST.get("loan") or 0).first()
        down = _decimal(request.POST.get("down_payment")) or Decimal("0")
        down_src = request.POST.get("down_source") or Funding.BANK
        down_acct = _bank_accounts().filter(pk=request.POST.get("down_account") or 0).first()
        loan_amount = _decimal(request.POST.get("loan_amount")) or Decimal("0")
        settle_financed_purchase(
            event, down_amount=down,
            down_source=down_src if down_src in Funding.values else Funding.BANK,
            down_account=down_acct, loan=loan, loan_amount=loan_amount, user=request.user,
        )
    else:
        src = request.POST.get("purchase_funding") or Funding.NONE
        event.funding_source = src if src in Funding.values else Funding.NONE
        event.funding_account = _bank_accounts().filter(
            pk=request.POST.get("purchase_account") or 0
        ).first() if event.funding_source == Funding.BANK else None
        event.save(update_fields=["funding_source", "funding_account"])
        save_cost_event(event, user=request.user, is_new=True)


def property_create(request):
    return _property_form(request, Property(), "create")


def property_edit(request, pk):
    return _property_form(request, get_object_or_404(Property, pk=pk), "edit")


def _property_form(request, property, mode):
    form = PropertyForm(request.POST or None, instance=property)
    error = ""
    if request.method == "POST":
        omode = request.POST.get("ownership_mode") or OwnershipMode.OWNED_CASH
        ptype = request.POST.get("property_type") or PropertyType.SINGLE_FAMILY
        use = request.POST.get("use") or PropertyUse.PRIMARY_RESIDENCE
        currency = (
            Currency.objects.filter(code=request.POST.get("currency") or "").first()
            or base_currency()
        )
        if form.is_valid() and omode in OwnershipMode.values:
            property = form.save(commit=False)
            property.ownership_mode = omode
            property.property_type = ptype if ptype in PropertyType.values \
                else PropertyType.SINGLE_FAMILY
            property.use = use if use in PropertyUse.values else PropertyUse.PRIMARY_RESIDENCE
            property.currency = currency
            property.seller_organization = _resolve_org(request, "seller_organization")
            from apps.relationships.services import parse_partial_dates

            for field, value in parse_partial_dates(request.POST, "acquired").items():
                setattr(property, field, value)
            property.save()
            ensure_gl_account(property)  # owned-only: every property carries a node
            _save_owners(request, property)
            if mode == "create":
                _maybe_acquisition(request, property)
            return redirect(tenant_url(request, f"realestate/{property.pk}/"))
        error = "Please complete the required fields."

    people = _household_people()
    current = list(property.owners.select_related("person").all()) if property.pk else []
    owner_rows = [
        {"id": o.person_id, "name": o.person.display_name, "tint": o.person.avatar_tint,
         "initials": o.person.initials, "role": o.role}
        for o in current
    ]
    ctx = realestate_context(
        request, "properties",
        form=form, property=property, mode=mode, error=error,
        ownership_modes=OwnershipMode.choices,
        property_types=PropertyType.choices,
        uses=PropertyUse.choices,
        owner_roles=OwnerRole.choices,
        currencies=Currency.objects.filter(is_active=True),
        base=base_currency(),
        people=people,
        owner_rows=owner_rows,
        seller=property.seller_organization,
        bank_accounts=_bank_accounts(),
        mortgage_loans=_mortgage_loans(),
        fundings=Funding.choices,
    )
    return render(request, "realestate/property_form.html", ctx)


def property_delete(request, pk):
    property = get_object_or_404(Property, pk=pk)
    if request.method == "POST":
        property.delete()  # plain soft-delete (bills/GL survive as history; loans precedent)
    return redirect(tenant_url(request, "realestate/all/"))


# --- Property detail ------------------------------------------------------------------------

def _value_geo(property):
    """Precomputed SVG geometry + latest figures for the value-over-time chart (empty geo when
    there's not enough history to draw a line). Mirrors automobile.views._value_geo."""
    data = appreciation_series(property)
    series = data["series"]
    if len(series) < 2:
        return {}, data
    dates = [d for d, _, _ in series]
    geo = line_chart_points(
        series, min_v=data["min"], max_v=data["max"], start=min(dates), end=max(dates)
    )
    return geo, data


def property_detail(request, pk):
    property = get_object_or_404(
        Property.objects.select_related("currency", "gl_account", "mortgage_loan"), pk=pk
    )
    events = list(
        property.cost_events.select_related("bill", "payment", "vendor_organization")
        .order_by("-date", "-id")
    )
    loan_summary = None
    if property.mortgage_loan_id:
        loan = property.mortgage_loan
        loan_summary = {"loan": loan, "balance": loan.balance}
    geo, value_data = _value_geo(property)
    ctx = realestate_context(
        request, "properties",
        property=property, base=base_currency(),
        events=events,
        owners=sorted(
            property.owners.select_related("person").all(), key=lambda o: o.role_order
        ),
        valuations=list(property.valuations.all()[:20]),
        value_geo=geo, value_data=value_data,
        documents=list(property.documents.all()),
        document_types=DocumentType.choices,
        history=property.history.all()[:60],
        loan_summary=loan_summary,
        cost_kinds=COST_PICKER_KINDS,
        disposal_methods=DisposalMethod.choices,
        bank_accounts=_bank_accounts(),
        credit_cards=_credit_cards(),
        cash_accounts=_cash_accounts(),
        fundings=Funding.choices,
        disposal=getattr(property, "disposal", None),
        organizations=Organization.objects.all(),
        today=datetime.date.today(),
    )
    return render(request, "realestate/property_detail.html", ctx)


# --- Cost events ----------------------------------------------------------------------------

def _apply_cost_funding(request, event):
    src = request.POST.get("funding_source") or Funding.NONE
    event.funding_source = src if src in Funding.values else Funding.NONE
    event.funding_account = event.credit_card = event.cash_account = None
    if event.funding_source == Funding.BANK:
        event.funding_account = _bank_accounts().filter(
            pk=request.POST.get("funding_account") or 0
        ).first()
        if event.funding_account is None:
            event.funding_source = Funding.NONE
    elif event.funding_source == Funding.CARD:
        event.credit_card = _credit_cards().filter(
            pk=request.POST.get("credit_card") or 0
        ).first()
        if event.credit_card is None:
            event.funding_source = Funding.NONE
    elif event.funding_source == Funding.CASH:
        event.cash_account = Account.objects.filter(
            pk=request.POST.get("cash_account") or 0, is_postable=True
        ).first()


def _apply_cost_post(request, event):
    kind = request.POST.get("kind", "")
    amount = _decimal(request.POST.get("amount"))
    date = parse_date(request.POST.get("date", "") or "")
    if kind not in CostKind.values or date is None or amount is None or amount <= 0:
        return None
    vendor = _resolve_org(request, "vendor_organization")
    if vendor is None:
        return None  # exactly-one vendor required
    event.kind = kind
    event.date = date
    event.amount = amount
    event.vendor_organization = vendor
    event.vendor_person = None
    event.memo = request.POST.get("memo", "").strip()
    event.reference = request.POST.get("reference", "").strip()
    event.due_date = parse_date(request.POST.get("due_date") or "") or None
    event.covers_from = parse_date(request.POST.get("covers_from") or "") or None
    event.covers_through = parse_date(request.POST.get("covers_through") or "") or None
    _apply_cost_funding(request, event)
    event.save()
    return event


def cost_create(request, pk):
    property = get_object_or_404(Property, pk=pk)
    if request.method == "POST":
        event = _apply_cost_post(request, PropertyCostEvent(property=property))
        if event is not None:
            save_cost_event(event, user=request.user, is_new=True)
    return redirect(tenant_url(request, f"realestate/{pk}/"))


def cost_edit(request, pk, ev):
    property = get_object_or_404(Property, pk=pk)
    event = get_object_or_404(PropertyCostEvent, pk=ev, property=property)
    if request.method == "POST" and _apply_cost_post(request, event) is not None:
        save_cost_event(event, user=request.user, is_new=False)
    return redirect(tenant_url(request, f"realestate/{pk}/"))


def cost_delete(request, pk, ev):
    property = get_object_or_404(Property, pk=pk)
    event = get_object_or_404(PropertyCostEvent, pk=ev, property=property)
    if request.method == "POST":
        try:
            delete_cost_event(event, user=request.user)
        except ValueError:
            pass  # a foreign payables payment is allocated — leave it, surface via the detail page
    return redirect(tenant_url(request, f"realestate/{pk}/"))


# --- Valuations -----------------------------------------------------------------------------

def valuation_add(request, pk):
    property = get_object_or_404(Property, pk=pk)
    if request.method == "POST":
        value = _decimal(request.POST.get("value"))
        as_of = parse_date(request.POST.get("as_of") or "") or datetime.date.today()
        if value is not None and value >= 0:
            PropertyValuation.objects.update_or_create(
                property=property, as_of=as_of,
                defaults={"value": value, "source": request.POST.get("source", "").strip()},
            )
    return redirect(tenant_url(request, f"realestate/{pk}/"))


def valuation_delete(request, pk, vid):
    property = get_object_or_404(Property, pk=pk)
    valuation = get_object_or_404(PropertyValuation, pk=vid, property=property)
    if request.method == "POST":
        valuation.delete()
    return redirect(tenant_url(request, f"realestate/{pk}/"))


# --- Disposal -------------------------------------------------------------------------------

def dispose(request, pk):
    property = get_object_or_404(Property, pk=pk)
    if request.method == "POST" and not hasattr(property, "disposal"):
        method = request.POST.get("method", "")
        date = parse_date(request.POST.get("date") or "") or datetime.date.today()
        if method in DisposalMethod.values:
            proceeds = _decimal(request.POST.get("proceeds")) or Decimal("0")
            proceeds_bank = _bank_accounts().filter(
                pk=request.POST.get("proceeds_account") or 0
            ).first()
            buyer = _resolve_org(request, "buyer_organization")
            disposal = PropertyDisposal(
                property=property, method=method, date=date, proceeds=proceeds,
                proceeds_account=proceeds_bank, buyer_organization=buyer,
                notes=request.POST.get("notes", "").strip(),
            )
            disposal.save()
            post_disposal(disposal, user=request.user)
    return redirect(tenant_url(request, f"realestate/{pk}/"))


# --- Documents ------------------------------------------------------------------------------

def document_upload(request, pk):
    property = get_object_or_404(Property, pk=pk)
    if request.method == "POST" and "document" in request.FILES:
        dtype = request.POST.get("doc_type") or DocumentType.OTHER
        doc = PropertyDocument(
            property=property,
            title=request.POST.get("title", "").strip() or request.FILES["document"].name,
            doc_type=dtype if dtype in DocumentType.values else DocumentType.OTHER,
            note=request.POST.get("note", "").strip(),
            file=request.FILES["document"],
        )
        doc.save()
    return redirect(tenant_url(request, f"realestate/{pk}/"))


def document_delete(request, pk, did):
    property = get_object_or_404(Property, pk=pk)
    doc = get_object_or_404(PropertyDocument, pk=did, property=property)
    if request.method == "POST":
        delete_document(doc)
    return redirect(tenant_url(request, f"realestate/{pk}/"))


# --- htmx fragments -------------------------------------------------------------------------

def org_search(request):
    q = request.GET.get("q", "").strip()
    orgs = Organization.objects.all()
    if q:
        orgs = orgs.filter(Q(name__icontains=q) | Q(display_name__icontains=q))
    return render(request, "realestate/partials/org_search.html", {"orgs": orgs[:8], "q": q})
