"""Health views (tenant-scoped, member-accessible). A dashboard (you-owe + overdue + recently paid +
outstanding-by-provider), a visit register + detail (Invoices / People / Documents / History tabs),
provider invoices (quick total or itemized EOB) with a partial-payment modal (bank / card / cash /
HSA), the dispute / write-off / refund actions, and an outstanding-by-provider view. Every money
movement goes through apps.health.services (locked payables bills / payments)."""

import datetime
from decimal import Decimal, InvalidOperation

from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_date

from apps.contacts.models import Person
from apps.finance.models import Account, AccountType
from apps.finance.services import base_currency
from apps.health.forms import EncounterForm, InvoiceForm, PrescriptionForm
from apps.health.models import (
    DocumentType,
    Encounter,
    EncounterProvider,
    EncounterSetting,
    EncounterType,
    Funding,
    HealthDocument,
    InvoiceCharge,
    InvoiceStatus,
    Prescription,
    ProviderInvoice,
    ProviderRole,
    VisitStatus,
)
from apps.health.services import (
    active_health_insurance,
    confirm_invoice,
    dashboard_stats,
    delete_document,
    delete_invoice,
    delete_invoice_payment,
    delete_prescription,
    delete_prescription_payment,
    dispute_invoice,
    hsa_summary,
    link_provider_affiliation,
    member_rollups,
    outstanding_by_provider,
    record_invoice_payment,
    record_prescription_payment,
    record_refund,
    record_visit_copay,
    reminders_due,
    resolve_dispute,
    save_encounter,
    save_invoice,
    save_prescription,
    total_unpaid,
    write_off_invoice,
)
from apps.organizations.models import Organization
from apps.tenants.models import Membership, Role

ENCOUNTER_SORTS = {
    "added": ("-id",),
    "date": ("date", "id"),
    "-date": ("-date", "-id"),
}


def tenant_url(request, path=""):
    return f"/t/{request.tenant.schema_name}/{path}"


def _is_owner(request) -> bool:
    return Membership.objects.filter(
        user=request.user, tenant=request.tenant, role=Role.OWNER
    ).exists()


def health_context(request, active, **extra):
    ctx = {
        "active": active,
        "is_owner": _is_owner(request),
        "nav_visits": Encounter.objects.count(),
        "nav_invoices": ProviderInvoice.objects.count(),
        "nav_prescriptions": Prescription.objects.count(),
    }
    ctx.update(extra)
    return ctx


def _decimal(raw):
    try:
        return Decimal((raw or "").strip())
    except (InvalidOperation, TypeError):
        return None


def _int(raw, default, *, lo=None, hi=None):
    try:
        val = int((raw or "").strip())
    except (ValueError, TypeError):
        return default
    if lo is not None:
        val = max(lo, val)
    if hi is not None:
        val = min(hi, val)
    return val


# --- shared pick-lists -----------------------------------------------------------------------

def _people():
    return Person.objects.all().order_by("last_name", "first_name")


def _patients():
    """Patients are household members only (you, family, dependents) — not external providers."""
    return Person.objects.filter(is_household_member=True).order_by("last_name", "first_name")


def _organizations():
    return Organization.objects.all().order_by("name")


def _bank_accounts():
    from apps.banking.models import BankAccount

    return BankAccount.objects.select_related("bank").all()


def _credit_cards():
    from apps.cards.models import CreditCard

    return CreditCard.objects.all()


def _cash_accounts():
    return Account.objects.filter(type=AccountType.ASSET, is_postable=True).order_by("code")


def _hsa_accounts():
    from apps.investments.models import InvestmentAccount

    return InvestmentAccount.objects.filter(registration="hsa", is_active=True).order_by("nickname")


# Organization categories treated as "medical" — the facility picker is limited to these so a visit
# is booked at a hospital / clinic / pharmacy, not any business in the household's contacts.
MEDICAL_ORG_CATEGORIES = ("Hospital/Clinic", "Pharmacy")


def _medical_organizations():
    """Organizations tagged with a medical category (facility picker source)."""
    return (
        Organization.objects.filter(categories__name__in=MEDICAL_ORG_CATEGORIES)
        .distinct().order_by("name")
    )


def _ensure_org_category(org, name) -> None:
    """Tag an organization with an ORG category (idempotent; no-op if the category is unseeded)."""
    from apps.setup.models import Category

    cat = Category.objects.filter(kind=Category.Kind.ORG, name=name).first()
    if cat:
        org.categories.add(cat)


def _ensure_person_category(person, name) -> None:
    """Tag a person with a PERSON category (idempotent; no-op if the category is unseeded)."""
    from apps.setup.models import Category

    cat = Category.objects.filter(kind=Category.Kind.PERSON, name=name).first()
    if cat:
        person.categories.add(cat)


def _split_name(raw):
    """Split a free-typed full name into (first, last); last may be blank for a single token."""
    parts = (raw or "").strip().split()
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _resolve_org(request, field, *, category_name=None):
    """A picked org id or an inline-created org by name (mirrors realestate / payables). A newly
    created org is tagged with `category_name` when given (e.g. a facility → Hospital/Clinic)."""
    new_name = request.POST.get(f"{field}_new_name", "").strip()
    if new_name:
        org = Organization.objects.create(name=new_name)
        if category_name:
            _ensure_org_category(org, category_name)
        return org
    oid = request.POST.get(field) or 0
    return Organization.objects.filter(pk=oid).first()


def _resolve_person(request, field):
    pid = request.POST.get(field) or 0
    return Person.objects.filter(pk=pid).first()


def _health_insurance_policies():
    """Active health-related insurance policies (medical / dental / vision) for the visit picker."""
    from apps.insurance.models import InsurancePolicy, PolicyStatus, PolicyType

    return (
        InsurancePolicy.objects.filter(
            status=PolicyStatus.ACTIVE,
            policy_type__in=[PolicyType.HEALTH, PolicyType.DENTAL, PolicyType.VISION],
        )
        .select_related("insurer_organization", "insurer_person")
        .order_by("policy_type", "-effective_date", "-id")
    )


def _resolve_health_policy(request, field):
    from apps.insurance.models import InsurancePolicy

    pid = request.POST.get(field) or 0
    return InsurancePolicy.objects.filter(pk=pid).first()


def _resolve_provider(request, field, *, category_name="Doctor"):
    """A picked person id or an inline-created provider by name. A newly created person is tagged
    with `category_name` (default Doctor) so it surfaces in Contacts as a provider."""
    new_name = request.POST.get(f"{field}_new_name", "").strip()
    if new_name:
        split = _split_name(new_name)
        if split:
            person = Person.objects.create(first_name=split[0], last_name=split[1])
            if category_name:
                _ensure_person_category(person, category_name)
            return person
    return _resolve_person(request, field)


def _form_context(request, **extra):
    ctx = {
        "people": _people(),
        "organizations": _organizations(),
        "encounter_types": EncounterType.choices,
        "settings": EncounterSetting.choices,
        "visit_statuses": VisitStatus.choices,
        "provider_roles": ProviderRole.choices,
        "invoice_statuses": InvoiceStatus.choices,
        "fundings": Funding.choices,
        "document_types": DocumentType.choices,
        "bank_accounts": _bank_accounts(),
        "credit_cards": _credit_cards(),
        "cash_accounts": _cash_accounts(),
        "hsa_accounts": _hsa_accounts(),
        "health_policies": _health_insurance_policies(),
        "base": base_currency(),
        "today": datetime.date.today(),
    }
    ctx.update(extra)
    return ctx


# --- Dashboard ------------------------------------------------------------------------------

def dashboard(request):
    stats = dashboard_stats()
    recent_visits = list(
        Encounter.objects.select_related("patient", "facility").order_by("-date", "-id")[:8]
    )
    overdue = [
        inv for inv in ProviderInvoice.objects.select_related(
            "biller_person", "biller_organization", "bill"
        )
        if inv.is_overdue
    ]
    overdue.sort(key=lambda i: i.due_date)
    ctx = health_context(
        request, "dashboard", base=base_currency(),
        recent_visits=recent_visits, overdue=overdue[:8],
        insurance_cards=active_health_insurance(),
        hsa=hsa_summary(), members=member_rollups(), reminders=reminders_due(within_days=60)[:6],
        facilities=_medical_organizations(),
        **stats,
    )
    return render(request, "health/dashboard.html", ctx)


# --- Reminders feed -------------------------------------------------------------------------

def reminders(request):
    rows = reminders_due(within_days=180)
    ctx = health_context(
        request, "reminders", base=base_currency(),
        reminders=rows,
        overdue_count=len([r for r in rows if r["days"] < 0]),
    )
    return render(request, "health/reminders.html", ctx)


# --- Providers (quick-create a doctor) ------------------------------------------------------

def provider_create(request):
    """Quick-create a provider (a Doctor-categorized Person) with an optional practice affiliation,
    from the dashboard. New people surface everywhere a provider can be picked + in Contacts."""
    if request.method == "POST":
        first = request.POST.get("first_name", "").strip()
        last = request.POST.get("last_name", "").strip()
        if first or last:
            person = Person.objects.create(first_name=first, last_name=last)
            _ensure_person_category(person, "Doctor")
            org = _resolve_org(request, "affiliation", category_name="Hospital/Clinic")
            if org is not None:
                link_provider_affiliation(person, org)
    return redirect(tenant_url(request, "health/"))


# --- Outstanding by provider ----------------------------------------------------------------

def providers(request):
    rows = outstanding_by_provider()
    ctx = health_context(
        request, "providers", base=base_currency(),
        rows=rows, total=total_unpaid(),
    )
    return render(request, "health/providers.html", ctx)


# --- Encounter register ---------------------------------------------------------------------

def encounter_list(request):
    qs = Encounter.objects.select_related("patient", "facility", "primary_provider")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(reason__icontains=q) | Q(notes__icontains=q)
            | Q(patient__first_name__icontains=q) | Q(patient__last_name__icontains=q)
            | Q(facility__name__icontains=q)
        ).distinct()
    etype = request.GET.get("type", "")
    if etype in EncounterType.values:
        qs = qs.filter(encounter_type=etype)
    sort = request.GET.get("sort", "-date")
    if sort not in ENCOUNTER_SORTS:
        sort = "-date"
    qs = qs.order_by(*ENCOUNTER_SORTS[sort])

    total = Encounter.objects.count()
    type_chips = [
        {"val": val, "label": label,
         "count": Encounter.objects.filter(encounter_type=val).count()}
        for val, label in EncounterType.choices
    ]
    page = Paginator(qs, 12).get_page(request.GET.get("page"))
    ctx = health_context(
        request, "visits", base=base_currency(),
        page=page, encounters=list(page.object_list), q=q, type=etype, sort=sort,
        total=total, type_chips=type_chips,
    )
    return render(request, "health/encounter_list.html", ctx)


def encounter_create(request):
    return _encounter_form(request, Encounter(), "create")


def encounter_edit(request, pk):
    return _encounter_form(request, get_object_or_404(Encounter, pk=pk), "edit")


def _apply_encounter(request, enc):
    etype = request.POST.get("encounter_type") or EncounterType.MEDICAL
    setting = request.POST.get("setting") or EncounterSetting.OFFICE
    vstatus = request.POST.get("visit_status") or VisitStatus.COMPLETED
    date = parse_date(request.POST.get("date") or "")
    patient = _resolve_person(request, "patient")
    if date is None or patient is None:
        return None
    enc.patient = patient
    enc.encounter_type = etype if etype in EncounterType.values else EncounterType.MEDICAL
    enc.setting = setting if setting in EncounterSetting.values else EncounterSetting.OFFICE
    enc.visit_status = vstatus if vstatus in VisitStatus.values else VisitStatus.COMPLETED
    enc.date = date
    enc.facility = _resolve_org(request, "facility", category_name="Hospital/Clinic")
    enc.primary_provider = _resolve_provider(request, "primary_provider")
    enc.plan = _resolve_health_policy(request, "plan")  # blank → save_encounter auto-links
    enc.reason = request.POST.get("reason", "").strip()
    enc.notes = request.POST.get("notes", "").strip()
    return enc


def _save_roster(request, enc):
    pids = request.POST.getlist("provider_person")
    roles = request.POST.getlist("provider_role")
    orgs = request.POST.getlist("provider_org")
    notes = request.POST.getlist("provider_note")
    enc.providers.all().delete()
    seen = set()
    order = 0
    for i, pid in enumerate(pids):
        role = roles[i] if i < len(roles) else ProviderRole.ATTENDING
        role = role if role in ProviderRole.values else ProviderRole.ATTENDING
        key = (pid, role)
        if not pid or key in seen:
            continue
        person = Person.objects.filter(pk=pid).first()
        if person is None:
            continue
        seen.add(key)
        org = Organization.objects.filter(pk=orgs[i] if i < len(orgs) else 0).first()
        EncounterProvider.objects.create(
            encounter=enc, person=person, role=role, organization=org,
            note=(notes[i] if i < len(notes) else "").strip(), order=order,
        )
        order += 1
        if org is not None:  # persist the doctor ↔ business affiliation
            link_provider_affiliation(person, org)


def _encounter_form(request, enc, mode):
    form = EncounterForm(request.POST or None, instance=enc)
    error = ""
    if request.method == "POST":
        if form.is_valid():
            enc = form.save(commit=False)
            if _apply_encounter(request, enc) is None:
                error = "Choose the patient and the visit date."
            else:
                copay = _decimal(request.POST.get("copay_amount"))
                want_copay = mode == "create" and copay and copay > 0
                if want_copay and enc.facility is None and enc.primary_provider is None:
                    error = "Add a facility or primary provider to record a copay at the visit."
                else:
                    enc.save()
                    _save_roster(request, enc)
                    save_encounter(enc, user=request.user, is_new=(mode == "create"))
                    if want_copay:
                        funding = request.POST.get("copay_funding") or Funding.BANK
                        if funding in Funding.values:
                            kw = _resolve_funding_target(request, funding)
                            try:
                                record_visit_copay(enc, amount=copay, funding=funding,
                                                   user=request.user, **kw)
                            except ValueError:
                                pass
                    return redirect(tenant_url(request, f"health/visits/{enc.pk}/"))
        else:
            error = "Please complete the required fields."
    facilities = list(_medical_organizations())
    if enc.facility_id and enc.facility not in facilities:
        facilities.insert(0, enc.facility)  # keep an already-set, non-medical facility selectable
    patients = list(_patients())
    if enc.patient_id and enc.patient not in patients:
        patients.insert(0, enc.patient)  # keep an already-set patient selectable on edit
    ctx = health_context(
        request, "visits", mode=mode, form=form, encounter=enc, error=error,
        roster=list(enc.providers.select_related("person", "organization").all()) if enc.pk else [],
        facilities=facilities, patients=patients,
        **_form_context(request),
    )
    return render(request, "health/encounter_form.html", ctx)


def encounter_delete(request, pk):
    enc = get_object_or_404(Encounter, pk=pk)
    if request.method == "POST":
        # Refuse if any invoice has a foreign Payables payment; otherwise erase its invoices' bills.
        try:
            with transaction.atomic():
                for inv in list(enc.invoices.all()):
                    delete_invoice(inv, user=request.user)
                enc.hard_delete()
        except ValueError:
            return redirect(tenant_url(request, f"health/visits/{pk}/"))
    return redirect(tenant_url(request, "health/visits/"))


# --- Encounter detail -----------------------------------------------------------------------

def encounter_detail(request, pk):
    enc = get_object_or_404(
        Encounter.objects.select_related("patient", "facility", "primary_provider"), pk=pk
    )
    invoices = list(
        enc.invoices.select_related("bill", "biller_person", "biller_organization",
                                    "rendering_provider").order_by("-invoice_date", "-id")
    )
    ctx = health_context(
        request, "visits", encounter=enc,
        invoices=invoices,
        roster=list(enc.providers.select_related("person", "organization").all()),
        documents=list(enc.documents.all()),
        history=enc.history.all()[:60],
        **_form_context(request),
    )
    return render(request, "health/encounter_detail.html", ctx)


# --- Invoices -------------------------------------------------------------------------------

def invoice_list(request):
    qs = ProviderInvoice.objects.select_related(
        "bill", "biller_person", "biller_organization", "encounter"
    )
    status = request.GET.get("status", "")
    if status in InvoiceStatus.values:
        qs = qs.filter(status=status)
    qs = qs.order_by("-invoice_date", "-id")
    total = ProviderInvoice.objects.count()
    status_chips = [
        {"val": val, "label": label,
         "count": ProviderInvoice.objects.filter(status=val).count()}
        for val, label in InvoiceStatus.choices
    ]
    page = Paginator(qs, 15).get_page(request.GET.get("page"))
    ctx = health_context(
        request, "invoices", base=base_currency(),
        page=page, invoices=list(page.object_list), status=status, total=total,
        status_chips=status_chips, total_owed=total_unpaid(),
    )
    return render(request, "health/invoice_list.html", ctx)


def _resolve_biller(request, inv):
    """Biller = an organization (search-select + inline-create) OR a person (select)."""
    org = _resolve_org(request, "biller_organization")
    if org is not None:
        inv.biller_organization = org
        inv.biller_person = None
        return
    person = _resolve_person(request, "biller_person")
    inv.biller_organization = None
    inv.biller_person = person


def _apply_invoice(request, inv):
    idate = parse_date(request.POST.get("invoice_date") or "")
    if idate is None:
        return None
    inv.invoice_date = idate
    inv.due_date = parse_date(request.POST.get("due_date") or "") or None
    status = request.POST.get("status") or InvoiceStatus.UNPAID
    inv.status = status if status in InvoiceStatus.values else InvoiceStatus.UNPAID
    inv.invoice_number = request.POST.get("invoice_number", "").strip()
    inv.reference = request.POST.get("reference", "").strip()
    inv.memo = request.POST.get("memo", "").strip()
    inv.amount_due = _decimal(request.POST.get("amount_due")) or Decimal("0")
    _resolve_biller(request, inv)
    inv.rendering_provider = _resolve_person(request, "rendering_provider")
    return inv


def _save_charges(request, inv):
    descs = request.POST.getlist("charge_description")
    if not any(d.strip() for d in descs):
        return  # no itemization supplied — keep the bare amount_due
    codes = request.POST.getlist("charge_code")
    billed = request.POST.getlist("charge_billed")
    allowed = request.POST.getlist("charge_allowed")
    ins_paid = request.POST.getlist("charge_insurance_paid")
    deduct = request.POST.getlist("charge_deductible")
    copay = request.POST.getlist("charge_copay")
    coins = request.POST.getlist("charge_coinsurance")
    noncov = request.POST.getlist("charge_noncovered")
    inv.charges.all().delete()

    def _d(lst, i):
        return _decimal(lst[i] if i < len(lst) else "") or Decimal("0")

    order = 0
    for i, desc in enumerate(descs):
        if not desc.strip():
            continue
        InvoiceCharge.objects.create(
            invoice=inv, description=desc.strip(), order=order,
            service_code=(codes[i] if i < len(codes) else "").strip(),
            billed=_d(billed, i), allowed=_d(allowed, i), insurance_paid=_d(ins_paid, i),
            deductible_amount=_d(deduct, i), copay_amount=_d(copay, i),
            coinsurance_amount=_d(coins, i), noncovered_amount=_d(noncov, i),
        )
        order += 1


def _invoice_form(request, inv, mode, *, encounter=None):
    form = InvoiceForm(request.POST or None, instance=inv)
    error = ""
    if request.method == "POST":
        if form.is_valid():
            inv = form.save(commit=False)
            if encounter is not None:
                inv.encounter = encounter
            if _apply_invoice(request, inv) is None:
                error = "Enter the invoice date."
            elif inv.status != InvoiceStatus.PENDING_INSURANCE and inv.biller is None:
                # A posted invoice needs a biller (the bill's vendor); don't persist an orphan row.
                error = "Choose the biller, or set the status to Pending insurance."
            else:
                inv.save()
                _save_charges(request, inv)
                save_invoice(inv, user=request.user, is_new=(mode == "create"))
                return redirect(tenant_url(request, f"health/invoices/{inv.pk}/"))
        else:
            error = "Please complete the required fields."
    ctx = health_context(
        request, "invoices", mode=mode, form=form, invoice=inv, encounter=encounter,
        error=error, charges=list(inv.charges.all()) if inv.pk else [],
        **_form_context(request),
    )
    return render(request, "health/invoice_form.html", ctx)


def invoice_create(request):
    return _invoice_form(request, ProviderInvoice(), "create")


def invoice_create_for_visit(request, pk):
    enc = get_object_or_404(Encounter, pk=pk)
    return _invoice_form(request, ProviderInvoice(encounter=enc), "create", encounter=enc)


def invoice_edit(request, pk):
    inv = get_object_or_404(ProviderInvoice, pk=pk)
    return _invoice_form(request, inv, "edit", encounter=inv.encounter)


def invoice_detail(request, pk):
    inv = get_object_or_404(
        ProviderInvoice.objects.select_related(
            "bill", "biller_person", "biller_organization", "rendering_provider", "encounter"
        ),
        pk=pk,
    )
    from apps.health.services import duplicate_warnings, invoice_payments

    payments = list(invoice_payments(inv)) if inv.bill_id is not None else []

    ctx = health_context(
        request, "invoices", invoice=inv,
        charges=list(inv.charges.all()),
        payments=payments,
        documents=list(inv.documents.all()),
        history=inv.history.all()[:60],
        warnings=duplicate_warnings(inv),
        **_form_context(request),
    )
    return render(request, "health/invoice_detail.html", ctx)


def invoice_delete(request, pk):
    inv = get_object_or_404(ProviderInvoice, pk=pk)
    enc_id = inv.encounter_id
    if request.method == "POST":
        try:
            delete_invoice(inv, user=request.user)
        except ValueError:
            return redirect(tenant_url(request, f"health/invoices/{pk}/"))
    dest = f"health/visits/{enc_id}/" if enc_id else "health/invoices/"
    return redirect(tenant_url(request, dest))


def invoice_confirm(request, pk):
    inv = get_object_or_404(ProviderInvoice, pk=pk)
    if request.method == "POST":
        amount = _decimal(request.POST.get("amount_due"))
        if amount is not None:
            inv.amount_due = amount
            inv.save(update_fields=["amount_due", "updated_at"])
        confirm_invoice(inv, user=request.user)
    return redirect(tenant_url(request, f"health/invoices/{pk}/"))


# --- Payments -------------------------------------------------------------------------------

def _resolve_funding_target(request, funding):
    if funding == Funding.BANK:
        return {"account": _bank_accounts().filter(pk=request.POST.get("funding_account") or 0)
                .first()}
    if funding == Funding.CARD:
        return {"card": _credit_cards().filter(pk=request.POST.get("credit_card") or 0).first()}
    if funding == Funding.CASH:
        return {"cash": Account.objects.filter(
            pk=request.POST.get("cash_account") or 0, is_postable=True).first()}
    if funding == Funding.HSA:
        return {"hsa": _hsa_accounts().filter(pk=request.POST.get("hsa_account") or 0).first()}
    return {}


def invoice_pay(request, pk):
    inv = get_object_or_404(ProviderInvoice, pk=pk)
    if request.method == "POST":
        amount = _decimal(request.POST.get("amount"))
        date = parse_date(request.POST.get("date") or "")
        funding = request.POST.get("funding") or Funding.BANK
        if amount and amount > 0 and date and funding in Funding.values and inv.bill_id:
            kw = _resolve_funding_target(request, funding)
            try:
                record_invoice_payment(inv, amount=amount, date=date, funding=funding,
                                       user=request.user, **kw)
            except ValueError:
                pass  # e.g. HSA chosen without an account — surface via the detail page
    return redirect(tenant_url(request, f"health/invoices/{pk}/"))


def invoice_payment_delete(request, pk, pay):
    inv = get_object_or_404(ProviderInvoice, pk=pk)
    from apps.health.services import invoice_payments

    payment = get_object_or_404(invoice_payments(inv), pk=pay)
    if request.method == "POST":
        delete_invoice_payment(inv, payment, user=request.user)
    return redirect(tenant_url(request, f"health/invoices/{pk}/"))


# --- Write-off / dispute / refund -----------------------------------------------------------

def invoice_writeoff(request, pk):
    inv = get_object_or_404(ProviderInvoice, pk=pk)
    if request.method == "POST":
        new_total = _decimal(request.POST.get("new_total")) or Decimal("0")
        try:
            write_off_invoice(inv, new_total=new_total, user=request.user)
        except ValueError:
            pass
    return redirect(tenant_url(request, f"health/invoices/{pk}/"))


def invoice_dispute(request, pk):
    inv = get_object_or_404(ProviderInvoice, pk=pk)
    if request.method == "POST":
        dispute_invoice(inv, user=request.user)
    return redirect(tenant_url(request, f"health/invoices/{pk}/"))


def invoice_resolve(request, pk):
    inv = get_object_or_404(ProviderInvoice, pk=pk)
    if request.method == "POST":
        resolve_dispute(inv, user=request.user)
    return redirect(tenant_url(request, f"health/invoices/{pk}/"))


def invoice_refund(request, pk):
    inv = get_object_or_404(ProviderInvoice, pk=pk)
    if request.method == "POST":
        amount = _decimal(request.POST.get("amount"))
        date = parse_date(request.POST.get("date") or "") or datetime.date.today()
        dest = request.POST.get("dest") or Funding.BANK
        if amount and amount > 0 and dest in Funding.values:
            kw = _resolve_funding_target(request, dest)
            mapped = {
                "account": "bank", "card": None, "cash": "cash", "hsa": "hsa",
            }
            call_kw = {}
            for k, v in kw.items():
                target = mapped.get(k)
                if target:
                    call_kw[target] = v
            try:
                record_refund(inv, amount=amount, dest=dest, date=date, user=request.user,
                              **call_kw)
            except ValueError:
                pass
    return redirect(tenant_url(request, f"health/invoices/{pk}/"))


# --- Prescriptions (P3) ---------------------------------------------------------------------

def prescription_list(request):
    qs = Prescription.objects.select_related(
        "bill", "pharmacy_organization", "patient", "prescriber_person"
    )
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(drug_name__icontains=q) | Q(dosage__icontains=q)
            | Q(patient__first_name__icontains=q) | Q(patient__last_name__icontains=q)
            | Q(pharmacy_organization__name__icontains=q)
        ).distinct()
    status = request.GET.get("status", "")
    if status in InvoiceStatus.values:
        qs = qs.filter(status=status)
    qs = qs.order_by("-date", "-id")
    total = Prescription.objects.count()
    page = Paginator(qs, 15).get_page(request.GET.get("page"))
    ctx = health_context(
        request, "prescriptions", base=base_currency(),
        page=page, prescriptions=list(page.object_list), q=q, status=status, total=total,
    )
    return render(request, "health/prescription_list.html", ctx)


def _apply_prescription(request, rx):
    date = parse_date(request.POST.get("date") or "")
    patient = _resolve_person(request, "patient")
    if date is None or patient is None:
        return None
    rx.patient = patient
    rx.date = date
    rx.drug_name = request.POST.get("drug_name", "").strip()
    rx.dosage = request.POST.get("dosage", "").strip()
    rx.prescriber_person = _resolve_provider(request, "prescriber_person")
    rx.pharmacy_organization = _resolve_org(
        request, "pharmacy_organization", category_name="Pharmacy"
    )
    rx.cost = _decimal(request.POST.get("cost")) or Decimal("0")
    rx.quantity = _int(request.POST.get("quantity"), None, lo=0)
    rx.days_supply = _int(request.POST.get("days_supply"), None, lo=0)
    rx.refills_remaining = _int(request.POST.get("refills_remaining"), 0, lo=0)
    rx.reference = request.POST.get("reference", "").strip()
    rx.memo = request.POST.get("memo", "").strip()
    return rx


def _prescription_form(request, rx, mode):
    form = PrescriptionForm(request.POST or None, instance=rx)
    error = ""
    if request.method == "POST":
        if form.is_valid():
            rx = form.save(commit=False)
            if _apply_prescription(request, rx) is None:
                error = "Choose the patient and the fill date."
            elif rx.cost > 0 and rx.pharmacy_organization is None:
                error = "Choose the pharmacy, or set the cost to 0."
            else:
                rx.save()
                save_prescription(rx, user=request.user, is_new=(mode == "create"))
                return redirect(tenant_url(request, f"health/prescriptions/{rx.pk}/"))
        else:
            error = "Please complete the required fields."
    patients = list(_patients())
    if rx.patient_id and rx.patient not in patients:
        patients.insert(0, rx.patient)  # keep an already-set patient selectable on edit
    ctx = health_context(
        request, "prescriptions", mode=mode, form=form, prescription=rx, error=error,
        patients=patients,
        **_form_context(request),
    )
    return render(request, "health/prescription_form.html", ctx)


def prescription_create(request):
    return _prescription_form(request, Prescription(), "create")


def prescription_edit(request, pk):
    return _prescription_form(request, get_object_or_404(Prescription, pk=pk), "edit")


def prescription_detail(request, pk):
    rx = get_object_or_404(
        Prescription.objects.select_related(
            "bill", "pharmacy_organization", "patient", "prescriber_person"
        ),
        pk=pk,
    )
    from apps.health.services import prescription_payments

    payments = list(prescription_payments(rx)) if rx.bill_id is not None else []
    ctx = health_context(
        request, "prescriptions", prescription=rx,
        payments=payments,
        documents=list(rx.documents.all()),
        history=rx.history.all()[:60],
        **_form_context(request),
    )
    return render(request, "health/prescription_detail.html", ctx)


def prescription_delete(request, pk):
    rx = get_object_or_404(Prescription, pk=pk)
    if request.method == "POST":
        try:
            delete_prescription(rx, user=request.user)
        except ValueError:
            return redirect(tenant_url(request, f"health/prescriptions/{pk}/"))
    return redirect(tenant_url(request, "health/prescriptions/"))


def prescription_pay(request, pk):
    rx = get_object_or_404(Prescription, pk=pk)
    if request.method == "POST":
        amount = _decimal(request.POST.get("amount"))
        date = parse_date(request.POST.get("date") or "")
        funding = request.POST.get("funding") or Funding.BANK
        if amount and amount > 0 and date and funding in Funding.values and rx.bill_id:
            kw = _resolve_funding_target(request, funding)
            try:
                record_prescription_payment(rx, amount=amount, date=date, funding=funding,
                                             user=request.user, **kw)
            except ValueError:
                pass
    return redirect(tenant_url(request, f"health/prescriptions/{pk}/"))


def prescription_payment_delete(request, pk, pay):
    rx = get_object_or_404(Prescription, pk=pk)
    from apps.health.services import prescription_payments

    payment = get_object_or_404(prescription_payments(rx), pk=pay)
    if request.method == "POST":
        delete_prescription_payment(rx, payment, user=request.user)
    return redirect(tenant_url(request, f"health/prescriptions/{pk}/"))


def prescription_document_upload(request, pk):
    rx = get_object_or_404(Prescription, pk=pk)
    _upload_document(request, prescription=rx)
    return redirect(tenant_url(request, f"health/prescriptions/{pk}/"))


# --- Plans & benefits (P2) ------------------------------------------------------------------

def _health_policy_types():
    from apps.insurance.models import PolicyType

    return [PolicyType.HEALTH, PolicyType.DENTAL, PolicyType.VISION]


def plans_list(request):
    from apps.health.services import active_health_plans, deductible_oop_status
    from apps.insurance.models import InsurancePolicy

    policies = list(
        InsurancePolicy.objects.filter(policy_type__in=_health_policy_types())
        .select_related("insurer_organization", "insurer_person", "health_plan")
        .order_by("-id")
    )
    rows = []
    for p in policies:
        hp = getattr(p, "health_plan", None)
        rows.append({"policy": p, "plan": hp,
                     "status": deductible_oop_status(p) if hp else None})
    ctx = health_context(
        request, "plans", base=base_currency(),
        rows=rows, plans_count=len(active_health_plans()),
    )
    return render(request, "health/plans_list.html", ctx)


def plan_edit(request, pk):
    from apps.health.models import HealthPlan
    from apps.insurance.models import InsurancePolicy

    policy = get_object_or_404(InsurancePolicy, pk=pk)
    hp = getattr(policy, "health_plan", None)
    if request.method == "POST":
        g = request.POST.get

        def _m(key):
            return _decimal(g(key)) or Decimal("0")

        defaults = {
            "plan_year_start_month": _int(g("plan_year_start_month"), 1, lo=1, hi=12),
            "plan_year_start_day": _int(g("plan_year_start_day"), 1, lo=1, hi=31),
            "network": g("network", "").strip(),
            "deductible_individual": _m("deductible_individual"),
            "deductible_family": _m("deductible_family"),
            "oop_max_individual": _m("oop_max_individual"),
            "oop_max_family": _m("oop_max_family"),
            "coinsurance_pct": _m("coinsurance_pct"),
            "dental_annual_max": _decimal(g("dental_annual_max")),
            "vision_allowance": _decimal(g("vision_allowance")),
        }
        hp, _ = HealthPlan.objects.update_or_create(policy=policy, defaults=defaults)
        _save_copay_rules(request, hp)
        return redirect(tenant_url(request, "health/plans/"))
    ctx = health_context(
        request, "plans", policy=policy, plan=hp,
        copay_rows=list(hp.copay_rules.all()) if hp else [],
        months=[(i, datetime.date(2000, i, 1).strftime("%B")) for i in range(1, 13)],
        **_form_context(request),
    )
    return render(request, "health/plan_form.html", ctx)


def _save_copay_rules(request, hp):
    from apps.health.models import CopayRule

    sts = request.POST.getlist("copay_service")
    amts = request.POST.getlist("copay_amount")
    notes = request.POST.getlist("copay_note")
    hp.copay_rules.all().delete()
    seen, order = set(), 0
    for i, raw in enumerate(sts):
        st = (raw or "").strip()
        if not st or st in seen:
            continue
        seen.add(st)
        CopayRule.objects.create(
            plan=hp, service_type=st,
            copay_amount=_decimal(amts[i] if i < len(amts) else "") or Decimal("0"),
            note=(notes[i] if i < len(notes) else "").strip(), order=order,
        )
        order += 1


# --- Documents ------------------------------------------------------------------------------

def _upload_document(request, *, encounter=None, invoice=None, prescription=None):
    if request.method == "POST" and "document" in request.FILES:
        dtype = request.POST.get("doc_type") or DocumentType.OTHER
        doc = HealthDocument(
            encounter=encounter, invoice=invoice, prescription=prescription,
            title=request.POST.get("title", "").strip() or request.FILES["document"].name,
            doc_type=dtype if dtype in DocumentType.values else DocumentType.OTHER,
            note=request.POST.get("note", "").strip(),
            file=request.FILES["document"],
        )
        doc.save()


def encounter_document_upload(request, pk):
    enc = get_object_or_404(Encounter, pk=pk)
    _upload_document(request, encounter=enc)
    return redirect(tenant_url(request, f"health/visits/{pk}/"))


def invoice_document_upload(request, pk):
    inv = get_object_or_404(ProviderInvoice, pk=pk)
    _upload_document(request, invoice=inv)
    return redirect(tenant_url(request, f"health/invoices/{pk}/"))


def document_delete(request, did):
    doc = get_object_or_404(HealthDocument, pk=did)
    if doc.encounter_id:
        dest = f"health/visits/{doc.encounter_id}/"
    elif doc.invoice_id:
        dest = f"health/invoices/{doc.invoice_id}/"
    elif doc.prescription_id:
        dest = f"health/prescriptions/{doc.prescription_id}/"
    else:
        dest = "health/"
    if request.method == "POST":
        delete_document(doc)
    return redirect(tenant_url(request, dest))
