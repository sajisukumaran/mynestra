"""Automobile (Vehicles) views (tenant-scoped, member-accessible). Mirrors the Loans idiom: a
dashboard, a vehicles list (search / ownership + type chips / sort / paginate), a vehicle detail
with cost register + service + odometer/fuel + documents + history tabs, and popup (c-modal) forms.
Every money movement goes through apps.automobile.services (locked payables bills/payments, or a
direct disposal entry); this layer reads POST, calls the service, and redirects."""

import datetime
from decimal import Decimal, InvalidOperation

from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_date

from apps.automobile.forms import (
    InspectionForm,
    PropertyTaxForm,
    RegistrationForm,
    VehicleForm,
)
from apps.automobile.models import (
    COMPLIANCE_DEFAULT_MONTHS,
    REGISTRATION_DEFAULT_MONTHS,
    ComplianceKind,
    ComplianceResult,
    CostKind,
    DisposalMethod,
    DriverRole,
    FuelType,
    FuelUnit,
    Funding,
    MileageUnit,
    OwnershipMode,
    PlateType,
    RegistrationReason,
    ServiceInvoiceCategory,
    ServiceSchedule,
    TitleStatus,
    Vehicle,
    VehicleCostEvent,
    VehicleDisposal,
    VehicleDriver,
    VehicleInspection,
    VehiclePropertyTax,
    VehicleRegistration,
    VehicleServiceInvoice,
    VehicleValuation,
)
from apps.automobile.services import (
    POSTING_ACTIVITIES,
    _add_months,
    cost_by_category,
    dashboard_stats,
    delete_cost_event,
    delete_inspection,
    delete_property_tax,
    delete_registration,
    delete_service_invoice,
    depreciation_series,
    ensure_gl_account,
    fuel_economy,
    mileage_log,
    post_disposal,
    register,
    renewals_due,
    save_cost_event,
    save_inspection,
    save_insurance_split,
    save_property_tax,
    save_registration,
    save_service_invoice,
    settle_financed_purchase,
    sync_driver_p2o,
)
from apps.contacts.models import Person
from apps.finance.models import Account, AccountType, Currency
from apps.finance.services import (
    base_currency,
    is_expert_mode,
    posting_map_for,
    set_posting_map,
)
from apps.investments.services import line_chart_points
from apps.organizations.models import Organization
from apps.tenants.models import Membership, Role

VEHICLE_SORTS = {
    "nickname": ("nickname", "id"),
    "-nickname": ("-nickname", "-id"),
    "added": ("created_at", "id"),
    "-added": ("-created_at", "-id"),
}

# Cost kinds offered in the register's "add cost" picker (purchase comes from the acquisition flow).
COST_PICKER_KINDS = [
    (CostKind.FUEL, "Fuel / charging"),
    (CostKind.SERVICE, "Service"),
    (CostKind.REPAIR, "Repair"),
    (CostKind.INSURANCE, "Insurance"),
    (CostKind.REGISTRATION, "Registration / road tax"),
    (CostKind.INSPECTION, "Inspection"),
    (CostKind.EMISSIONS, "Emissions / smog"),
    (CostKind.LEASE_PAYMENT, "Lease payment"),
    (CostKind.LEASE_DEPOSIT, "Lease deposit"),
    (CostKind.IMPROVEMENT, "Improvement / upgrade"),
    (CostKind.TAX_FEE, "Tax / fee"),
    (CostKind.PROPERTY_TAX, "Personal property tax"),
    (CostKind.OTHER, "Other"),
]


def tenant_url(request, path=""):
    return f"/t/{request.tenant.schema_name}/{path}"


def _is_owner(request) -> bool:
    return Membership.objects.filter(
        user=request.user, tenant=request.tenant, role=Role.OWNER
    ).exists()


def automobile_context(request, active, **extra):
    ctx = {
        "active": active,
        "is_owner": _is_owner(request),
        "nav_vehicles": Vehicle.objects.count(),
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


def _expense_accounts():
    return Account.objects.filter(type=AccountType.EXPENSE, is_postable=True).order_by("code")


def _auto_loans():
    from apps.loans.models import Loan

    return Loan.objects.filter(is_active=True)


# --- Dashboard ------------------------------------------------------------------------------

def dashboard(request):
    stats = dashboard_stats()
    vehicles = stats["vehicles"]
    # Cost-by-category donut + cost-by-vehicle bars across the fleet.
    from apps.investments.services import Slice, donut_segments

    cat_totals: dict = {}
    veh_bars = []
    for v in vehicles:
        segs, total = cost_by_category(v)
        for seg in segs:
            cat_totals[seg["label"]] = cat_totals.get(seg["label"], Decimal("0")) + seg["value"]
        if total > 0:
            veh_bars.append({"label": v.nickname, "value": total, "tint": v.type_tint})
    palette = ["amber", "teal", "sky", "violet", "rose", "emerald", "indigo", "slate"]
    cat_slices = [
        Slice(label, value, palette[i % len(palette)])
        for i, (label, value) in enumerate(
            sorted(cat_totals.items(), key=lambda kv: kv[1], reverse=True)
        )
    ]
    donut = donut_segments(cat_slices)
    donut_total = sum((s.value for s in cat_slices), Decimal("0"))
    veh_bars.sort(key=lambda b: b["value"], reverse=True)
    bars_total = sum((b["value"] for b in veh_bars), Decimal("0"))
    recent = list(
        VehicleCostEvent.objects.select_related("vehicle").order_by("-date", "-id")[:8]
    )
    ctx = automobile_context(
        request, "dashboard", base=base_currency(),
        donut_segments=donut, donut_total=donut_total,
        bar_items=veh_bars, bar_total=bars_total,
        renewals=renewals_due(within_days=90), recent=recent, **stats,
    )
    return render(request, "automobile/dashboard.html", ctx)


# --- Vehicle list ---------------------------------------------------------------------------

def vehicle_list(request):
    qs = Vehicle.objects.select_related("currency", "gl_account")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(nickname__icontains=q) | Q(make__icontains=q) | Q(model_name__icontains=q)
            | Q(vin__icontains=q) | Q(license_plate__icontains=q)
        ).distinct()

    mode = request.GET.get("mode", "")
    if mode in OwnershipMode.values:
        qs = qs.filter(ownership_mode=mode)
    fuel = request.GET.get("fuel", "")
    if fuel in FuelType.values:
        qs = qs.filter(fuel_type=fuel)

    sort = request.GET.get("sort", "nickname")
    if sort not in VEHICLE_SORTS:
        sort = "nickname"
    qs = qs.order_by(*VEHICLE_SORTS[sort])

    total = Vehicle.objects.count()
    mode_chips = [
        {"val": val, "label": label, "count": Vehicle.objects.filter(ownership_mode=val).count()}
        for val, label in OwnershipMode.choices
    ]
    page = Paginator(qs, 12).get_page(request.GET.get("page"))
    ctx = automobile_context(
        request, "vehicles",
        page=page, vehicles=list(page.object_list), q=q, mode=mode, fuel=fuel, sort=sort,
        sort_name_next="-nickname" if sort == "nickname" else "nickname",
        sort_added_next="-added" if sort == "added" else "added",
        total=total, mode_chips=mode_chips, base=base_currency(),
    )
    return render(request, "automobile/vehicle_list.html", ctx)


# --- Vehicle create / edit / delete ---------------------------------------------------------

def _resolve_org(request, field):
    """A picked org id or an inline-created org by name (mirrors payables _resolve_vendor)."""
    new_name = request.POST.get(f"{field}_new_name", "").strip()
    if new_name:
        return Organization.objects.create(name=new_name)
    oid = request.POST.get(field) or 0
    return Organization.objects.filter(pk=oid).first()


def _save_drivers(request, vehicle):
    pids = request.POST.getlist("driver_person")
    roles = request.POST.getlist("driver_role")
    vehicle.drivers.all().delete()
    seen = set()
    for pid, role in zip(pids, roles, strict=False):
        if not pid or pid in seen:
            continue
        seen.add(pid)
        person = Person.objects.filter(pk=pid).first()
        if person is None:
            continue
        VehicleDriver.objects.create(
            vehicle=vehicle, person=person,
            role=role if role in DriverRole.values else DriverRole.ADDITIONAL_DRIVER,
        )


def _save_posting_maps(request, vehicle):
    for act in POSTING_ACTIVITIES:
        acct_id = request.POST.get(f"map_{act['key']}") or None
        chosen = Account.objects.filter(pk=acct_id, is_postable=True).first() if acct_id else None
        set_posting_map(vehicle, act["key"], chosen)


def _apply_lease_terms(request, vehicle):
    vehicle.lease_monthly_payment = _decimal(request.POST.get("lease_monthly_payment"))
    vehicle.lease_start_date = parse_date(request.POST.get("lease_start_date") or "") or None
    vehicle.lease_end_date = parse_date(request.POST.get("lease_end_date") or "") or None
    vehicle.lease_term_months = _int(request.POST.get("lease_term_months"))
    vehicle.lease_annual_mileage = _int(request.POST.get("lease_annual_mileage"))
    vehicle.lease_residual = _decimal(request.POST.get("lease_residual"))
    deposit = _decimal(request.POST.get("lease_security_deposit"))
    vehicle.lease_security_deposit = deposit or Decimal("0")


def _maybe_acquisition(request, vehicle):
    """On create only: record the purchase (owned) via the service layer. A financed vehicle uses
    settle_financed_purchase (down payment + loan); an owned-cash vehicle a single funded/unfunded
    purchase bill. Runs once; a leased vehicle records no purchase."""
    if vehicle.is_leased:
        return
    price = _decimal(request.POST.get("purchase_price"))
    if price is None or price <= 0:
        return
    on = parse_date(request.POST.get("purchase_date") or "") or datetime.date.today()
    dealer = vehicle.dealer_organization
    odo = _int(request.POST.get("initial_odometer"))
    event = VehicleCostEvent(
        vehicle=vehicle, kind=CostKind.PURCHASE, date=on, amount=price,
        vendor_organization=dealer, vendor_person=None, odometer=odo,
    )
    if dealer is None:
        return  # exactly-one vendor required; skip the purchase if no dealer chosen
    event.save()
    if vehicle.is_financed:
        loan = _auto_loans().filter(pk=request.POST.get("loan") or 0).first()
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


def _seed_initial_registration(request, vehicle):
    """On create only: if a plate / jurisdiction / registration expiry was entered on the vehicle
    form, seed one INITIAL registration term (records become the source of truth thereafter)."""
    plate = (request.POST.get("license_plate") or "").strip()
    jurisdiction = (request.POST.get("plate_jurisdiction") or "").strip()
    expires = parse_date(request.POST.get("registration_expiry") or "") or None
    if not (plate or jurisdiction or expires):
        return
    on = datetime.date.today()
    if vehicle.acquired.is_set and vehicle.acquired.year:
        on = datetime.date(
            vehicle.acquired.year, vehicle.acquired.month or 1, vehicle.acquired.day or 1
        )
    reg = VehicleRegistration(
        vehicle=vehicle, jurisdiction=jurisdiction, plate_number=plate,
        title_number=(request.POST.get("title_number") or "").strip(),
        effective_from=on, expires_on=expires, reason=RegistrationReason.INITIAL,
    )
    save_registration(reg, user=request.user)  # no fee — the plate was recorded, not paid here


def vehicle_create(request):
    return _vehicle_form(request, Vehicle(), "create")


def vehicle_edit(request, pk):
    return _vehicle_form(request, get_object_or_404(Vehicle, pk=pk), "edit")


def _vehicle_form(request, vehicle, mode):
    form = VehicleForm(request.POST or None, instance=vehicle)
    expert = is_expert_mode()
    error = ""
    if request.method == "POST":
        omode = request.POST.get("ownership_mode") or OwnershipMode.OWNED_CASH
        currency = (
            Currency.objects.filter(code=request.POST.get("currency") or "").first()
            or base_currency()
        )
        if form.is_valid() and omode in OwnershipMode.values:
            vehicle = form.save(commit=False)
            vehicle.ownership_mode = omode
            vehicle.currency = currency
            vehicle.fuel_type = (
                request.POST.get("fuel_type")
                if request.POST.get("fuel_type") in FuelType.values else FuelType.GASOLINE
            )
            vehicle.mileage_unit = (
                request.POST.get("mileage_unit")
                if request.POST.get("mileage_unit") in MileageUnit.values else MileageUnit.MILES
            )
            vehicle.dealer_organization = _resolve_org(request, "dealer_organization")
            vehicle.insurer_organization = _resolve_org(request, "insurer_organization")
            vehicle.insurance_expiry = parse_date(
                request.POST.get("insurance_expiry") or ""
            ) or None
            vehicle.registration_expiry = parse_date(
                request.POST.get("registration_expiry") or ""
            ) or None
            vehicle.inspection_due = parse_date(request.POST.get("inspection_due") or "") or None
            vehicle.warranty_expiry = parse_date(request.POST.get("warranty_expiry") or "") or None
            vehicle.warranty_miles = _int(request.POST.get("warranty_miles"))
            vehicle.inspection_exempt = request.POST.get("inspection_exempt") in ("on", "1", "true")
            vehicle.emissions_exempt = request.POST.get("emissions_exempt") in ("on", "1", "true")
            if omode == OwnershipMode.LEASED:
                _apply_lease_terms(request, vehicle)
            from apps.relationships.services import parse_partial_dates

            for field, value in parse_partial_dates(request.POST, "acquired").items():
                setattr(vehicle, field, value)
            vehicle.save()
            if vehicle.is_owned:
                ensure_gl_account(vehicle)
            if expert:
                _save_posting_maps(request, vehicle)
            _save_drivers(request, vehicle)
            sync_driver_p2o(vehicle)
            if mode == "create":
                _maybe_acquisition(request, vehicle)
                _seed_initial_registration(request, vehicle)
            return redirect(tenant_url(request, f"automobile/{vehicle.pk}/"))
        error = "Please complete the required fields."

    people = Person.objects.filter(is_household_member=True)
    current = list(vehicle.drivers.select_related("person").all()) if vehicle.pk else []
    driver_rows = [
        {"id": d.person_id, "name": d.person.display_name, "tint": d.person.avatar_tint,
         "initials": d.person.initials, "role": d.role}
        for d in current
    ]
    pmap = posting_map_for(vehicle) if vehicle.pk else {}
    posting_activities = [
        {**act, "current": pmap.get(act["key"], "")} for act in POSTING_ACTIVITIES
    ]
    ctx = automobile_context(
        request, "vehicles",
        form=form, vehicle=vehicle, mode=mode, error=error,
        ownership_modes=OwnershipMode.choices,
        fuel_types=FuelType.choices,
        mileage_units=MileageUnit.choices,
        driver_roles=DriverRole.choices,
        currencies=Currency.objects.filter(is_active=True),
        base=base_currency(),
        people=people,
        driver_rows=driver_rows,
        dealer=vehicle.dealer_organization,
        insurer=vehicle.insurer_organization,
        bank_accounts=_bank_accounts(),
        auto_loans=_auto_loans(),
        fundings=Funding.choices,
        expert=expert,
        posting_activities=posting_activities,
        expense_accounts=_expense_accounts(),
    )
    return render(request, "automobile/vehicle_form.html", ctx)


def vehicle_delete(request, pk):
    vehicle = get_object_or_404(Vehicle, pk=pk)
    if request.method == "POST":
        vehicle.delete()  # plain soft-delete (bills/GL survive as history; loans precedent)
    return redirect(tenant_url(request, "automobile/all/"))


# --- Vehicle detail -------------------------------------------------------------------------

def _value_geo(vehicle):
    data = depreciation_series(vehicle)
    series = data["series"]
    if len(series) < 2:
        return {}, data
    dates = [d for d, _, _ in series]
    geo = line_chart_points(
        series, min_v=data["min"], max_v=data["max"], start=min(dates), end=max(dates)
    )
    return geo, data


def vehicle_detail(request, pk):
    vehicle = get_object_or_404(
        Vehicle.objects.select_related("currency", "gl_account", "loan"), pk=pk
    )
    geo, value_data = _value_geo(vehicle)
    loan_summary = None
    if vehicle.is_financed and vehicle.loan_id:
        from apps.loans.amortization import payoff_projection

        proj = payoff_projection(vehicle.loan)
        loan_summary = {
            "loan": vehicle.loan, "balance": vehicle.loan.balance,
            "payoff_date": proj.get("payoff_date"),
        }
    donut, donut_total = cost_by_category(vehicle)
    today = datetime.date.today()
    ctx = automobile_context(
        request, "vehicles",
        vehicle=vehicle, base=base_currency(),
        rows=register(vehicle),
        drivers=sorted(vehicle.drivers.select_related("person").all(), key=lambda d: d.role_order),
        schedules=list(vehicle.service_schedules.all()),
        readings=mileage_log(vehicle),
        economy=fuel_economy(vehicle),
        valuations=list(vehicle.valuations.all()[:20]),
        history=vehicle.history.all()[:60],
        value_geo=geo, value_data=value_data,
        loan_summary=loan_summary,
        donut_segments=donut, donut_total=donut_total,
        cost_kinds=COST_PICKER_KINDS,
        disposal_methods=DisposalMethod.choices,
        fuel_units=FuelUnit.choices,
        bank_accounts=_bank_accounts(),
        credit_cards=_credit_cards(),
        cash_accounts=_cash_accounts(),
        fundings=Funding.choices,
        disposal=getattr(vehicle, "disposal", None),
        # Registration / tax / compliance tab.
        registrations=list(vehicle.registrations.select_related("lienholder_organization").all()),
        inspections=list(vehicle.inspections.select_related("station_organization").all()),
        property_taxes=list(vehicle.property_taxes.all()),
        current_registration=vehicle.current_registration,
        service_invoices=list(
            vehicle.service_invoices.select_related(
                "vendor_person", "vendor_organization", "bill"
            ).prefetch_related("jobs__parts").all()
        ),
        plate_types=PlateType.choices,
        title_statuses=TitleStatus.choices,
        registration_reasons=RegistrationReason.choices,
        compliance_kinds=ComplianceKind.choices,
        compliance_results=ComplianceResult.choices,
        service_categories=ServiceInvoiceCategory.choices,
        organizations=Organization.objects.all(),
        # Server-side next-due pre-fills (user-editable in the modals).
        today=today,
        reg_expires_default=_add_months(today, REGISTRATION_DEFAULT_MONTHS),
        safety_expires_default=_add_months(today, COMPLIANCE_DEFAULT_MONTHS[ComplianceKind.SAFETY]),
        emissions_expires_default=_add_months(
            today, COMPLIANCE_DEFAULT_MONTHS[ComplianceKind.EMISSIONS]
        ),
        tax_year_default=today.year,
    )
    return render(request, "automobile/vehicle_detail.html", ctx)


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
    event.covers_through = parse_date(request.POST.get("covers_through") or "") or None
    event.odometer = _int(request.POST.get("odometer"))
    event.fuel_volume = _decimal(request.POST.get("fuel_volume"))
    event.fuel_unit = (
        request.POST.get("fuel_unit") if request.POST.get("fuel_unit") in FuelUnit.values else ""
    )
    event.is_full_tank = request.POST.get("is_full_tank") in ("on", "1", "true")
    _apply_cost_funding(request, event)
    event.save()
    return event


def cost_create(request, pk):
    vehicle = get_object_or_404(Vehicle, pk=pk)
    if request.method == "POST":
        event = _apply_cost_post(request, VehicleCostEvent(vehicle=vehicle))
        if event is not None:
            save_cost_event(event, user=request.user, is_new=True)
    return redirect(tenant_url(request, f"automobile/{pk}/"))


def cost_edit(request, pk, ev):
    vehicle = get_object_or_404(Vehicle, pk=pk)
    event = get_object_or_404(VehicleCostEvent, pk=ev, vehicle=vehicle)
    if request.method == "POST" and _apply_cost_post(request, event) is not None:
        save_cost_event(event, user=request.user, is_new=False)
    return redirect(tenant_url(request, f"automobile/{pk}/"))


def cost_delete(request, pk, ev):
    vehicle = get_object_or_404(Vehicle, pk=pk)
    event = get_object_or_404(VehicleCostEvent, pk=ev, vehicle=vehicle)
    if request.method == "POST":
        try:
            delete_cost_event(event, user=request.user)
        except ValueError:
            pass  # a foreign payables payment is allocated — leave it, surface via the detail page
    return redirect(tenant_url(request, f"automobile/{pk}/"))


# --- Registration / inspection / property-tax records ---------------------------------------

def _apply_fee(request):
    """Parse the optional fee/vendor/funding block shared by the registration + inspection record
    modals. Returns a fee dict (amount / vendor / funding / due/ref/memo) or None when no fee was
    entered (or no vendor was chosen — a bill needs exactly one vendor)."""
    from types import SimpleNamespace

    amount = _decimal(request.POST.get("fee_amount"))
    if amount is None or amount <= 0:
        return None
    vendor = _resolve_org(request, "fee_vendor_organization")
    if vendor is None:
        return None
    shim = SimpleNamespace(
        funding_source=Funding.NONE, funding_account=None, credit_card=None, cash_account=None
    )
    _apply_cost_funding(request, shim)
    return {
        "amount": amount, "vendor_organization": vendor, "vendor_person": None,
        "reference": request.POST.get("fee_reference", "").strip(),
        "memo": request.POST.get("fee_memo", "").strip(),
        "due_date": parse_date(request.POST.get("fee_due_date") or "") or None,
        "funding_source": shim.funding_source, "funding_account": shim.funding_account,
        "credit_card": shim.credit_card, "cash_account": shim.cash_account,
    }


def registration_add(request, pk):
    vehicle = get_object_or_404(Vehicle, pk=pk)
    if request.method == "POST":
        form = RegistrationForm(request.POST, instance=VehicleRegistration(vehicle=vehicle))
        if form.is_valid():
            reg = form.save(commit=False)
            reg.vehicle = vehicle
            reg.lienholder_organization = _resolve_org(request, "lienholder_organization")
            if "document" in request.FILES:
                reg.document = request.FILES["document"]
            save_registration(reg, fee=_apply_fee(request), user=request.user)
    return redirect(tenant_url(request, f"automobile/{pk}/"))


def registration_edit(request, pk, rid):
    vehicle = get_object_or_404(Vehicle, pk=pk)
    reg = get_object_or_404(VehicleRegistration, pk=rid, vehicle=vehicle)
    if request.method == "POST":
        form = RegistrationForm(request.POST, instance=reg)
        if form.is_valid():
            reg = form.save(commit=False)
            reg.lienholder_organization = _resolve_org(request, "lienholder_organization")
            if "document" in request.FILES:
                reg.document = request.FILES["document"]
            save_registration(reg, fee=_apply_fee(request), user=request.user)
    return redirect(tenant_url(request, f"automobile/{pk}/"))


def registration_delete(request, pk, rid):
    vehicle = get_object_or_404(Vehicle, pk=pk)
    reg = get_object_or_404(VehicleRegistration, pk=rid, vehicle=vehicle)
    if request.method == "POST":
        try:
            delete_registration(reg, user=request.user)
        except ValueError:
            pass  # a foreign payables payment is allocated to the fee bill — leave it
    return redirect(tenant_url(request, f"automobile/{pk}/"))


def inspection_add(request, pk):
    vehicle = get_object_or_404(Vehicle, pk=pk)
    if request.method == "POST":
        form = InspectionForm(request.POST, instance=VehicleInspection(vehicle=vehicle))
        if form.is_valid():
            insp = form.save(commit=False)
            insp.vehicle = vehicle
            insp.station_organization = _resolve_org(request, "station_organization")
            if "document" in request.FILES:
                insp.document = request.FILES["document"]
            save_inspection(insp, fee=_apply_fee(request), user=request.user)
    return redirect(tenant_url(request, f"automobile/{pk}/"))


def inspection_edit(request, pk, iid):
    vehicle = get_object_or_404(Vehicle, pk=pk)
    insp = get_object_or_404(VehicleInspection, pk=iid, vehicle=vehicle)
    if request.method == "POST":
        form = InspectionForm(request.POST, instance=insp)
        if form.is_valid():
            insp = form.save(commit=False)
            insp.station_organization = _resolve_org(request, "station_organization")
            if "document" in request.FILES:
                insp.document = request.FILES["document"]
            save_inspection(insp, fee=_apply_fee(request), user=request.user)
    return redirect(tenant_url(request, f"automobile/{pk}/"))


def inspection_delete(request, pk, iid):
    vehicle = get_object_or_404(Vehicle, pk=pk)
    insp = get_object_or_404(VehicleInspection, pk=iid, vehicle=vehicle)
    if request.method == "POST":
        try:
            delete_inspection(insp, user=request.user)
        except ValueError:
            pass
    return redirect(tenant_url(request, f"automobile/{pk}/"))


def _property_tax_fee(request, pt):
    """The property-tax fee dict — the tax amount IS the bill, so a vendor (taxing authority) is
    required. Returns None if no vendor was chosen (the view then skips posting)."""
    from types import SimpleNamespace

    vendor = _resolve_org(request, "fee_vendor_organization")
    if vendor is None:
        return None
    shim = SimpleNamespace(
        funding_source=Funding.NONE, funding_account=None, credit_card=None, cash_account=None
    )
    _apply_cost_funding(request, shim)
    return {
        "amount": pt.amount, "vendor_organization": vendor, "vendor_person": None,
        "reference": request.POST.get("fee_reference", "").strip(),
        "memo": request.POST.get("fee_memo", "").strip(),
        "due_date": pt.due_date,
        "funding_source": shim.funding_source, "funding_account": shim.funding_account,
        "credit_card": shim.credit_card, "cash_account": shim.cash_account,
    }


def property_tax_add(request, pk):
    vehicle = get_object_or_404(Vehicle, pk=pk)
    if request.method == "POST":
        form = PropertyTaxForm(request.POST, instance=VehiclePropertyTax(vehicle=vehicle))
        if form.is_valid():
            pt = form.save(commit=False)
            pt.vehicle = vehicle
            fee = _property_tax_fee(request, pt)
            if fee is not None:
                if "document" in request.FILES:
                    pt.document = request.FILES["document"]
                save_property_tax(pt, fee=fee, user=request.user)
    return redirect(tenant_url(request, f"automobile/{pk}/"))


def property_tax_edit(request, pk, tid):
    vehicle = get_object_or_404(Vehicle, pk=pk)
    pt = get_object_or_404(VehiclePropertyTax, pk=tid, vehicle=vehicle)
    if request.method == "POST":
        form = PropertyTaxForm(request.POST, instance=pt)
        if form.is_valid():
            pt = form.save(commit=False)
            fee = _property_tax_fee(request, pt)
            if fee is not None:
                if "document" in request.FILES:
                    pt.document = request.FILES["document"]
                save_property_tax(pt, fee=fee, user=request.user)
    return redirect(tenant_url(request, f"automobile/{pk}/"))


def property_tax_delete(request, pk, tid):
    vehicle = get_object_or_404(Vehicle, pk=pk)
    pt = get_object_or_404(VehiclePropertyTax, pk=tid, vehicle=vehicle)
    if request.method == "POST":
        try:
            delete_property_tax(pt, user=request.user)
        except ValueError:
            pass
    return redirect(tenant_url(request, f"automobile/{pk}/"))


def title_release(request, pk):
    """Manual 'Mark title released' action — appends a clean-title registration term on payoff."""
    from apps.automobile.services import release_title_lien

    vehicle = get_object_or_404(Vehicle, pk=pk)
    if request.method == "POST":
        release_title_lien(vehicle, user=request.user)
    return redirect(tenant_url(request, f"automobile/{pk}/"))


# --- Service invoices (multi-line: header → jobs → parts) -----------------------------------

def _parse_service_jobs(request):
    """Parse the indexed job/part arrays (e.g. `job[0][part][2][unit_price]`) into a jobs list of
    dicts, each with a nested `parts` list. Jobs/parts with no meaningful content are dropped."""
    jobs = []
    j = 0
    while f"job[{j}][code]" in request.POST or f"job[{j}][complaint]" in request.POST \
            or f"job[{j}][labor_amount]" in request.POST:
        prefix = f"job[{j}]"
        code = (request.POST.get(f"{prefix}[code]") or "").strip()
        complaint = (request.POST.get(f"{prefix}[complaint]") or "").strip()
        description = (request.POST.get(f"{prefix}[description]") or "").strip()
        technician = (request.POST.get(f"{prefix}[technician]") or "").strip()
        labor_hours = _decimal(request.POST.get(f"{prefix}[labor_hours]"))
        labor_amount = _decimal(request.POST.get(f"{prefix}[labor_amount]")) or Decimal("0")
        parts = []
        p = 0
        while f"{prefix}[part][{p}][part_number]" in request.POST \
                or f"{prefix}[part][{p}][description]" in request.POST \
                or f"{prefix}[part][{p}][unit_price]" in request.POST:
            pp = f"{prefix}[part][{p}]"
            part_number = (request.POST.get(f"{pp}[part_number]") or "").strip()
            pdesc = (request.POST.get(f"{pp}[description]") or "").strip()
            qty = _decimal(request.POST.get(f"{pp}[quantity]")) or Decimal("1")
            unit_price = _decimal(request.POST.get(f"{pp}[unit_price]")) or Decimal("0")
            if part_number or pdesc or unit_price > 0:
                parts.append({
                    "part_number": part_number, "description": pdesc,
                    "quantity": qty, "unit_price": unit_price,
                })
            p += 1
        if code or complaint or description or labor_amount > 0 or parts:
            jobs.append({
                "code": code, "complaint": complaint, "description": description,
                "technician": technician, "labor_hours": labor_hours,
                "labor_amount": labor_amount, "parts": parts,
            })
        j += 1
    return jobs


def _apply_service_invoice_post(request, inv):
    """Parse the service-invoice header onto `inv` (vendor, funding, totals, meta). Returns the jobs
    structure, or None when required header fields are missing (date + vendor)."""
    date = parse_date(request.POST.get("date") or "")
    vendor = _resolve_org(request, "vendor_organization")
    if date is None or vendor is None:
        return None
    inv.date = date
    inv.vendor_organization = vendor
    inv.vendor_person = None
    inv.invoice_number = request.POST.get("invoice_number", "").strip()
    inv.service_advisor = request.POST.get("service_advisor", "").strip()
    inv.odometer_in = _int(request.POST.get("odometer_in"))
    inv.odometer_out = _int(request.POST.get("odometer_out"))
    cat = request.POST.get("category") or ServiceInvoiceCategory.SERVICE
    inv.category = cat if cat in ServiceInvoiceCategory.values else ServiceInvoiceCategory.SERVICE
    inv.sublet = _decimal(request.POST.get("sublet")) or Decimal("0")
    inv.shop_supplies = _decimal(request.POST.get("shop_supplies")) or Decimal("0")
    inv.discount = _decimal(request.POST.get("discount")) or Decimal("0")
    inv.sales_tax = _decimal(request.POST.get("sales_tax")) or Decimal("0")
    inv.reference = request.POST.get("reference", "").strip()
    inv.memo = request.POST.get("memo", "").strip()
    if "document" in request.FILES:
        inv.document = request.FILES["document"]
    _apply_cost_funding(request, inv)
    return _parse_service_jobs(request)


def service_invoice_add(request, pk):
    vehicle = get_object_or_404(Vehicle, pk=pk)
    if request.method == "POST":
        inv = VehicleServiceInvoice(vehicle=vehicle)
        jobs = _apply_service_invoice_post(request, inv)
        if jobs is not None:
            save_service_invoice(inv, jobs, user=request.user)
    return redirect(tenant_url(request, f"automobile/{pk}/"))


def service_invoice_edit(request, pk, sid):
    vehicle = get_object_or_404(Vehicle, pk=pk)
    inv = get_object_or_404(VehicleServiceInvoice, pk=sid, vehicle=vehicle)
    if request.method == "POST":
        jobs = _apply_service_invoice_post(request, inv)
        if jobs is not None:
            save_service_invoice(inv, jobs, user=request.user)
    return redirect(tenant_url(request, f"automobile/{pk}/"))


def service_invoice_delete(request, pk, sid):
    vehicle = get_object_or_404(Vehicle, pk=pk)
    inv = get_object_or_404(VehicleServiceInvoice, pk=sid, vehicle=vehicle)
    if request.method == "POST":
        try:
            delete_service_invoice(inv, user=request.user)
        except ValueError:
            pass
    return redirect(tenant_url(request, f"automobile/{pk}/"))


# --- Valuation / odometer / service ---------------------------------------------------------

def valuation_add(request, pk):
    vehicle = get_object_or_404(Vehicle, pk=pk)
    if request.method == "POST":
        value = _decimal(request.POST.get("value"))
        as_of = parse_date(request.POST.get("as_of") or "") or datetime.date.today()
        if value is not None and value >= 0:
            VehicleValuation.objects.update_or_create(
                vehicle=vehicle, as_of=as_of,
                defaults={"value": value, "source": request.POST.get("source", "").strip()},
            )
    return redirect(tenant_url(request, f"automobile/{pk}/"))


def odometer_add(request, pk):
    from apps.automobile.models import OdometerReading
    from apps.automobile.services import _recompute_denorms

    vehicle = get_object_or_404(Vehicle, pk=pk)
    if request.method == "POST":
        mileage = _int(request.POST.get("mileage"))
        as_of = parse_date(request.POST.get("as_of") or "") or datetime.date.today()
        if mileage is not None:
            OdometerReading.objects.update_or_create(
                vehicle=vehicle, as_of=as_of,
                defaults={"mileage": mileage, "source": OdometerReading.Source.MANUAL,
                          "note": request.POST.get("note", "").strip()},
            )
            _recompute_denorms(vehicle)
    return redirect(tenant_url(request, f"automobile/{pk}/"))


def service_add(request, pk):
    vehicle = get_object_or_404(Vehicle, pk=pk)
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        if name:
            ServiceSchedule.objects.create(
                vehicle=vehicle, name=name,
                interval_months=_int(request.POST.get("interval_months")),
                interval_miles=_int(request.POST.get("interval_miles")),
                next_due_date=parse_date(request.POST.get("next_due_date") or "") or None,
                next_due_mileage=_int(request.POST.get("next_due_mileage")),
            )
    return redirect(tenant_url(request, f"automobile/{pk}/"))


def service_edit(request, pk, sid):
    vehicle = get_object_or_404(Vehicle, pk=pk)
    sched = get_object_or_404(ServiceSchedule, pk=sid, vehicle=vehicle)
    if request.method == "POST":
        if request.POST.get("_delete"):
            sched.delete()
        else:
            sched.name = request.POST.get("name", "").strip() or sched.name
            sched.interval_months = _int(request.POST.get("interval_months"))
            sched.interval_miles = _int(request.POST.get("interval_miles"))
            sched.next_due_date = parse_date(request.POST.get("next_due_date") or "") or None
            sched.next_due_mileage = _int(request.POST.get("next_due_mileage"))
            sched.is_active = request.POST.get("is_active") in ("on", "1", "true")
            sched.save()
    return redirect(tenant_url(request, f"automobile/{pk}/"))


# --- Disposal -------------------------------------------------------------------------------

def dispose(request, pk):
    vehicle = get_object_or_404(Vehicle, pk=pk)
    if request.method == "POST" and not hasattr(vehicle, "disposal"):
        method = request.POST.get("method", "")
        date = parse_date(request.POST.get("date") or "") or datetime.date.today()
        if method in DisposalMethod.values:
            proceeds = _decimal(request.POST.get("proceeds")) or Decimal("0")
            proceeds_bank = _bank_accounts().filter(
                pk=request.POST.get("proceeds_account") or 0
            ).first()
            buyer = _resolve_org(request, "buyer_organization")
            disposal = VehicleDisposal(
                vehicle=vehicle, method=method, date=date, proceeds=proceeds,
                odometer=_int(request.POST.get("odometer")),
                proceeds_account=proceeds_bank, buyer_organization=buyer,
                notes=request.POST.get("notes", "").strip(),
            )
            disposal.save()
            trade_bill = None
            if method == DisposalMethod.TRADE_IN:
                replacement = Vehicle.objects.filter(
                    pk=request.POST.get("replacement") or 0
                ).first()
                trade_bill = _open_purchase_bill(replacement) if replacement else None
            post_disposal(disposal, trade_bill=trade_bill, user=request.user)
    return redirect(tenant_url(request, f"automobile/{pk}/"))


def _open_purchase_bill(vehicle):
    """The replacement vehicle's open purchase bill (via its purchase cost event), or None."""
    ev = vehicle.cost_events.filter(kind=CostKind.PURCHASE, bill__isnull=False).first()
    if ev and ev.bill and ev.bill.status in ("open", "partially_paid"):
        return ev.bill
    return None


# --- Multi-vehicle insurance ----------------------------------------------------------------

def insurance_create(request):
    if request.method == "POST":
        insurer = _resolve_org(request, "insurer_organization")
        date = parse_date(request.POST.get("date") or "") or datetime.date.today()
        vids = request.POST.getlist("split_vehicle")
        amounts = request.POST.getlist("split_amount")
        throughs = request.POST.getlist("split_through")
        rows = []
        for i, vid in enumerate(vids):
            amt = _decimal(amounts[i] if i < len(amounts) else "")
            veh = Vehicle.objects.filter(pk=vid or 0).first()
            if veh and amt and amt > 0:
                rows.append({
                    "vehicle": veh, "amount": amt,
                    "covers_through": parse_date(
                        throughs[i] if i < len(throughs) else ""
                    ) or None,
                })
        if insurer and rows:
            src = request.POST.get("funding_source") or Funding.NONE
            save_insurance_split(
                rows, insurer_organization=insurer, date=date,
                reference=request.POST.get("reference", "").strip(),
                funding_source=src if src in Funding.values else Funding.NONE,
                funding_account=_bank_accounts().filter(
                    pk=request.POST.get("funding_account") or 0
                ).first(),
                user=request.user,
            )
    return redirect(tenant_url(request, "automobile/"))


# --- htmx fragments -------------------------------------------------------------------------

def value_chart_fragment(request, pk):
    vehicle = get_object_or_404(Vehicle, pk=pk)
    geo, value_data = _value_geo(vehicle)
    return render(
        request, "automobile/partials/value_chart.html",
        {"vehicle": vehicle, "value_geo": geo, "value_data": value_data, "base": base_currency()},
    )


def vendor_search(request):
    q = request.GET.get("q", "").strip()
    orgs = Organization.objects.all()
    if q:
        orgs = orgs.filter(Q(name__icontains=q) | Q(display_name__icontains=q))
    return render(
        request, "automobile/partials/vendor_search.html", {"orgs": orgs[:8], "q": q}
    )


def driver_search(request):
    q = request.GET.get("q", "").strip()
    people = Person.objects.all()
    if q:
        people = people.filter(
            Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(preferred_name__icontains=q)
        )
    return render(
        request, "automobile/partials/driver_search.html", {"candidates": people[:8], "q": q}
    )
