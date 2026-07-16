"""Insurance views (tenant-scoped, member-accessible). Mirrors the Automobile idiom: a dashboard, a
policy list (search / type + status chips / sort / paginate), a policy detail with coverages /
members / assets / premiums / history tabs, and popup (c-modal) forms. Every money movement goes
through apps.insurance.services (locked payables bills/payments); this layer reads POST, calls the
service, and redirects."""

import datetime
from decimal import Decimal, InvalidOperation

from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.dateparse import parse_date

from apps.contacts.models import Person
from apps.finance.models import Account, Currency
from apps.finance.services import base_currency
from apps.insurance.forms import PolicyForm
from apps.insurance.models import (
    POLICY_TYPE_TINT,
    Claim,
    ClaimStatus,
    Funding,
    InsurancePolicy,
    InsurancePremium,
    MemberRole,
    PayoutDestination,
    PolicyCoverage,
    PolicyMember,
    PolicyStatus,
    PolicyType,
    PremiumFrequency,
    SettlementKind,
)
from apps.insurance.services import (
    claims_overview,
    dashboard_stats,
    delete_claim,
    delete_premium,
    save_claim,
    save_premium,
    set_claim_vehicle,
    set_covered_vehicles,
    sync_policy_p2o,
    void_claim,
)
from apps.organizations.models import Organization
from apps.tenants.models import Membership, Role

POLICY_SORTS = {
    "added": ("-id",),
    "expiry": ("expiry_date", "id"),
    "-expiry": ("-expiry_date", "-id"),
}


def tenant_url(request, path=""):
    return f"/t/{request.tenant.schema_name}/{path}"


def _is_owner(request) -> bool:
    return Membership.objects.filter(
        user=request.user, tenant=request.tenant, role=Role.OWNER
    ).exists()


def insurance_context(request, active, **extra):
    ctx = {
        "active": active,
        "is_owner": _is_owner(request),
        "nav_policies": InsurancePolicy.objects.count(),
        "nav_claims": Claim.objects.count(),
    }
    ctx.update(extra)
    return ctx


def _decimal(raw):
    try:
        return Decimal((raw or "").strip())
    except (InvalidOperation, TypeError):
        return None


def _bank_accounts():
    from apps.banking.models import BankAccount

    return BankAccount.objects.select_related("bank").all()


def _credit_cards():
    from apps.cards.models import CreditCard

    return CreditCard.objects.all()


def _cash_accounts():
    from apps.finance.models import AccountType

    return Account.objects.filter(type=AccountType.ASSET, is_postable=True).order_by("code")


def _household_people():
    return Person.objects.filter(is_household_member=True)


def _vehicles():
    from apps.automobile.models import Vehicle

    return Vehicle.objects.filter(is_active=True).order_by("nickname")


def _expense_accounts():
    """Postable expense accounts — the loss-expense home a reimbursement credits."""
    from apps.finance.models import AccountType

    return Account.objects.filter(type=AccountType.EXPENSE, is_postable=True).order_by("code")


def _covered_vehicles(policy):
    """Vehicles this policy covers (incl. already-disposed ones, for editing a total-loss claim)."""
    from django.contrib.contenttypes.models import ContentType

    from apps.automobile.models import Vehicle

    ct = ContentType.objects.get_for_model(Vehicle)
    ids = list(policy.assets.filter(content_type=ct).values_list("object_id", flat=True))
    if not ids:
        return Vehicle.objects.none()
    return Vehicle.objects.filter(pk__in=ids).order_by("nickname")


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
    policies = stats["policies"]
    by_type: dict = {}
    for p in policies:
        if not p.is_active:
            continue
        row = by_type.setdefault(
            p.policy_type,
            {"label": p.get_policy_type_display(), "value": Decimal("0"),
             "tint": POLICY_TYPE_TINT.get(p.policy_type, "slate")},
        )
        row["value"] += p.annualized_premium
    bar_items = sorted(by_type.values(), key=lambda b: b["value"], reverse=True)
    bars_total = sum((b["value"] for b in bar_items), Decimal("0"))
    recent = list(
        InsurancePremium.objects.select_related("policy").order_by("-date", "-id")[:8]
    )
    ctx = insurance_context(
        request, "dashboard", base=base_currency(),
        bar_items=bar_items, bar_total=bars_total,
        recent=recent, **stats,
    )
    return render(request, "insurance/dashboard.html", ctx)


# --- Policy list ----------------------------------------------------------------------------

def policy_list(request):
    qs = InsurancePolicy.objects.select_related(
        "insurer_organization", "insurer_person", "currency"
    )
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(nickname__icontains=q) | Q(plan_name__icontains=q)
            | Q(policy_number__icontains=q)
            | Q(insurer_organization__name__icontains=q)
        ).distinct()

    ptype = request.GET.get("type", "")
    if ptype in PolicyType.values:
        qs = qs.filter(policy_type=ptype)
    status = request.GET.get("status", "")
    if status in PolicyStatus.values:
        qs = qs.filter(status=status)

    sort = request.GET.get("sort", "added")
    if sort not in POLICY_SORTS:
        sort = "added"
    qs = qs.order_by(*POLICY_SORTS[sort])

    total = InsurancePolicy.objects.count()
    type_chips = [
        {"val": val, "label": label,
         "count": InsurancePolicy.objects.filter(policy_type=val).count()}
        for val, label in PolicyType.choices
    ]
    page = Paginator(qs, 12).get_page(request.GET.get("page"))
    ctx = insurance_context(
        request, "policies",
        page=page, policies=list(page.object_list), q=q, type=ptype, status=status, sort=sort,
        sort_expiry_next="-expiry" if sort == "expiry" else "expiry",
        total=total, type_chips=type_chips, statuses=PolicyStatus.choices, base=base_currency(),
    )
    return render(request, "insurance/policy_list.html", ctx)


# --- Policy create / edit / delete ----------------------------------------------------------

def _save_coverages(request, policy):
    types = request.POST.getlist("coverage_type")
    limits = request.POST.getlist("coverage_limit")
    deducts = request.POST.getlist("coverage_deductible")
    premiums = request.POST.getlist("coverage_premium")
    notes = request.POST.getlist("coverage_note")
    policy.coverages.all().delete()
    order = 0
    for i, raw_type in enumerate(types):
        ctype = (raw_type or "").strip()
        if not ctype:
            continue
        PolicyCoverage.objects.create(
            policy=policy, coverage_type=ctype, order=order,
            limit_amount=_decimal(limits[i] if i < len(limits) else ""),
            deductible_amount=_decimal(deducts[i] if i < len(deducts) else ""),
            premium_portion=_decimal(premiums[i] if i < len(premiums) else ""),
            note=(notes[i] if i < len(notes) else "").strip(),
        )
        order += 1


def _save_members(request, policy):
    pids = request.POST.getlist("member_person")
    roles = request.POST.getlist("member_role")
    percents = request.POST.getlist("member_percent")
    mnotes = request.POST.getlist("member_note")
    policy.members.all().delete()
    seen = set()
    for i, pid in enumerate(pids):
        role = roles[i] if i < len(roles) else MemberRole.INSURED
        role = role if role in MemberRole.values else MemberRole.INSURED
        key = (pid, role)
        if not pid or key in seen:
            continue
        person = Person.objects.filter(pk=pid).first()
        if person is None:
            continue
        seen.add(key)
        PolicyMember.objects.create(
            policy=policy, person=person, role=role,
            beneficiary_percent=_decimal(percents[i] if i < len(percents) else ""),
            relationship_note=(mnotes[i] if i < len(mnotes) else "").strip(),
        )


def _save_covered_vehicles(request, policy):
    ids = [i for i in request.POST.getlist("covered_vehicle") if i]
    vehicles = list(_vehicles().filter(pk__in=ids)) if ids else []
    set_covered_vehicles(policy, vehicles)


def policy_create(request):
    return _policy_form(request, InsurancePolicy(), "create")


def policy_edit(request, pk):
    return _policy_form(request, get_object_or_404(InsurancePolicy, pk=pk), "edit")


def _policy_form(request, policy, mode):
    form = PolicyForm(request.POST or None, instance=policy)
    error = ""
    if request.method == "POST":
        ptype = request.POST.get("policy_type") or PolicyType.AUTO
        status = request.POST.get("status") or PolicyStatus.ACTIVE
        freq = request.POST.get("premium_frequency") or PremiumFrequency.ANNUAL
        currency = (
            Currency.objects.filter(code=request.POST.get("currency") or "").first()
            or base_currency()
        )
        if form.is_valid() and ptype in PolicyType.values:
            policy = form.save(commit=False)
            policy.policy_type = ptype
            policy.status = status if status in PolicyStatus.values else PolicyStatus.ACTIVE
            policy.currency = currency
            policy.insurer_organization = _resolve_org(request, "insurer_organization")
            policy.insurer_person = None
            policy.effective_date = parse_date(request.POST.get("effective_date") or "") or None
            policy.expiry_date = parse_date(request.POST.get("expiry_date") or "") or None
            policy.premium_amount = _decimal(request.POST.get("premium_amount")) or Decimal("0")
            policy.premium_frequency = (
                freq if freq in PremiumFrequency.values else PremiumFrequency.ANNUAL
            )
            policy.save()
            _save_coverages(request, policy)
            _save_members(request, policy)
            _save_covered_vehicles(request, policy)
            sync_policy_p2o(policy)
            return redirect(tenant_url(request, f"insurance/policies/{policy.pk}/"))
        error = "Please complete the required fields."

    coverage_rows = list(policy.coverages.all()) if policy.pk else []
    member_rows = list(policy.members.select_related("person").all()) if policy.pk else []
    covered_ids = (
        set(policy.assets.values_list("object_id", flat=True)) if policy.pk else set()
    )
    ctx = insurance_context(
        request, "policies",
        form=form, policy=policy, mode=mode, error=error,
        policy_types=PolicyType.choices,
        statuses=PolicyStatus.choices,
        frequencies=PremiumFrequency.choices,
        member_roles=MemberRole.choices,
        currencies=Currency.objects.filter(is_active=True),
        base=base_currency(),
        people=_household_people(),
        insurer=policy.insurer_organization,
        vehicles=_vehicles(),
        covered_ids=covered_ids,
        coverage_rows=coverage_rows,
        member_rows=member_rows,
    )
    return render(request, "insurance/policy_form.html", ctx)


def policy_delete(request, pk):
    policy = get_object_or_404(InsurancePolicy, pk=pk)
    if request.method == "POST":
        policy.delete()  # plain soft-delete (premium bills/GL survive as history)
    return redirect(tenant_url(request, "insurance/policies/"))


# --- Policy detail --------------------------------------------------------------------------

def policy_detail(request, pk):
    policy = get_object_or_404(
        InsurancePolicy.objects.select_related(
            "insurer_organization", "insurer_person", "currency"
        ),
        pk=pk,
    )
    premiums = list(
        policy.premiums.select_related("bill", "payment").order_by("-date", "-id")
    )
    assets = list(policy.assets.select_related("content_type").all())
    claims = list(
        policy.claims.select_related("disposal", "journal_entry", "loss_expense_account")
        .order_by("-loss_date", "-id")
    )
    covered_vehicles = list(_covered_vehicles(policy))
    ctx = insurance_context(
        request, "policies",
        policy=policy, base=base_currency(),
        coverages=list(policy.coverages.all()),
        members=sorted(policy.members.select_related("person").all(), key=lambda m: m.role),
        assets=assets,
        premiums=premiums,
        claims=claims,
        history=policy.history.all()[:60],
        frequencies=PremiumFrequency.choices,
        fundings=Funding.choices,
        bank_accounts=_bank_accounts(),
        credit_cards=_credit_cards(),
        cash_accounts=_cash_accounts(),
        expense_accounts=_expense_accounts(),
        covered_vehicles=covered_vehicles,
        can_total_loss=bool(covered_vehicles),
        claim_statuses=ClaimStatus.choices,
        settlement_kinds=SettlementKind.choices,
        payout_destinations=PayoutDestination.choices,
        today=datetime.date.today(),
    )
    return render(request, "insurance/policy_detail.html", ctx)


# --- Premiums -------------------------------------------------------------------------------

def _apply_premium_funding(request, premium):
    src = request.POST.get("funding_source") or Funding.NONE
    premium.funding_source = src if src in Funding.values else Funding.NONE
    premium.funding_account = premium.credit_card = premium.cash_account = None
    if premium.funding_source == Funding.BANK:
        premium.funding_account = _bank_accounts().filter(
            pk=request.POST.get("funding_account") or 0
        ).first()
        if premium.funding_account is None:
            premium.funding_source = Funding.NONE
    elif premium.funding_source == Funding.CARD:
        premium.credit_card = _credit_cards().filter(
            pk=request.POST.get("credit_card") or 0
        ).first()
        if premium.credit_card is None:
            premium.funding_source = Funding.NONE
    elif premium.funding_source == Funding.CASH:
        premium.cash_account = Account.objects.filter(
            pk=request.POST.get("cash_account") or 0, is_postable=True
        ).first()


def _apply_premium_post(request, premium):
    amount = _decimal(request.POST.get("amount"))
    date = parse_date(request.POST.get("date") or "")
    if date is None or amount is None or amount <= 0:
        return None
    premium.date = date
    premium.amount = amount
    premium.covers_from = parse_date(request.POST.get("covers_from") or "") or None
    premium.covers_through = parse_date(request.POST.get("covers_through") or "") or None
    premium.due_date = parse_date(request.POST.get("due_date") or "") or None
    premium.reference = request.POST.get("reference", "").strip()
    premium.memo = request.POST.get("memo", "").strip()
    _apply_premium_funding(request, premium)
    premium.save()
    return premium


def premium_create(request, pk):
    policy = get_object_or_404(InsurancePolicy, pk=pk)
    if request.method == "POST" and policy.insurer is not None:
        premium = _apply_premium_post(request, InsurancePremium(policy=policy))
        if premium is not None:
            save_premium(premium, user=request.user, is_new=True)
    return redirect(tenant_url(request, f"insurance/policies/{pk}/"))


def premium_edit(request, pk, prem):
    policy = get_object_or_404(InsurancePolicy, pk=pk)
    premium = get_object_or_404(InsurancePremium, pk=prem, policy=policy)
    if request.method == "POST" and _apply_premium_post(request, premium) is not None:
        save_premium(premium, user=request.user, is_new=False)
    return redirect(tenant_url(request, f"insurance/policies/{pk}/"))


def premium_delete(request, pk, prem):
    policy = get_object_or_404(InsurancePolicy, pk=pk)
    premium = get_object_or_404(InsurancePremium, pk=prem, policy=policy)
    if request.method == "POST":
        try:
            delete_premium(premium, user=request.user)
        except ValueError:
            pass  # a foreign payables payment is allocated — leave it, surface via the detail page
    return redirect(tenant_url(request, f"insurance/policies/{pk}/"))


# --- Claims ---------------------------------------------------------------------------------

def _apply_claim_payout_dest(request, claim):
    dest = request.POST.get("payout_destination") or PayoutDestination.NONE
    claim.payout_destination = dest if dest in PayoutDestination.values else PayoutDestination.NONE
    claim.bank_account = claim.cash_account = None
    if claim.payout_destination == PayoutDestination.BANK:
        claim.bank_account = _bank_accounts().filter(
            pk=request.POST.get("bank_account") or 0
        ).first()
        if claim.bank_account is None:
            claim.payout_destination = PayoutDestination.NONE
    elif claim.payout_destination == PayoutDestination.CASH:
        # None cash account → the direct entry falls back to Cash on Hand (1110).
        claim.cash_account = Account.objects.filter(
            pk=request.POST.get("cash_account") or 0, is_postable=True
        ).first()


def _build_claim(request, claim, policy):
    """Populate a Claim from POST (does NOT save). `settlement_kind` is fixed after creation.
    Returns None if the required loss date is missing."""
    loss_date = parse_date(request.POST.get("loss_date") or "")
    if loss_date is None:
        return None
    claim.loss_date = loss_date
    claim.claim_number = request.POST.get("claim_number", "").strip()
    claim.reported_date = parse_date(request.POST.get("reported_date") or "") or None
    status = request.POST.get("status") or ClaimStatus.OPEN
    claim.status = status if status in ClaimStatus.values else ClaimStatus.OPEN
    if claim.pk is None:
        sk = request.POST.get("settlement_kind") or SettlementKind.REIMBURSEMENT
        claim.settlement_kind = sk if sk in SettlementKind.values else SettlementKind.REIMBURSEMENT
    claim.deductible_amount = _decimal(request.POST.get("deductible_amount")) or Decimal("0")
    claim.payout_amount = _decimal(request.POST.get("payout_amount")) or Decimal("0")
    claim.payout_date = parse_date(request.POST.get("payout_date") or "") or None
    claim.notes = request.POST.get("notes", "").strip()
    _apply_claim_payout_dest(request, claim)
    if claim.is_total_loss:
        claim.loss_expense_account = None
    else:
        claim.loss_expense_account = _expense_accounts().filter(
            pk=request.POST.get("loss_expense_account") or 0
        ).first()
    vid = request.POST.get("claimed_vehicle") or 0
    vehicle = _covered_vehicles(policy).filter(pk=vid).first() if vid else None
    set_claim_vehicle(claim, vehicle)
    return claim


def claim_create(request, pk):
    policy = get_object_or_404(InsurancePolicy, pk=pk)
    if request.method == "POST":
        claim = _build_claim(request, Claim(policy=policy), policy)
        if claim is not None:
            try:
                with transaction.atomic():
                    claim.save()
                    save_claim(claim, user=request.user, is_new=True)
            except ValueError:
                pass  # invalid (e.g. total-loss without an available vehicle) — nothing persisted
    return redirect(tenant_url(request, f"insurance/policies/{pk}/"))


def claim_edit(request, pk, cid):
    policy = get_object_or_404(InsurancePolicy, pk=pk)
    claim = get_object_or_404(Claim, pk=cid, policy=policy)
    if request.method == "POST":
        built = _build_claim(request, claim, policy)
        if built is not None:
            try:
                with transaction.atomic():
                    claim.save()
                    save_claim(claim, user=request.user, is_new=False)
            except ValueError:
                pass
    return redirect(tenant_url(request, f"insurance/policies/{pk}/"))


def claim_void(request, pk, cid):
    policy = get_object_or_404(InsurancePolicy, pk=pk)
    claim = get_object_or_404(Claim, pk=cid, policy=policy)
    if request.method == "POST":
        void_claim(claim, user=request.user)
    return redirect(tenant_url(request, f"insurance/policies/{pk}/"))


def claim_delete(request, pk, cid):
    policy = get_object_or_404(InsurancePolicy, pk=pk)
    claim = get_object_or_404(Claim, pk=cid, policy=policy)
    if request.method == "POST":
        delete_claim(claim, user=request.user)
    return redirect(tenant_url(request, f"insurance/policies/{pk}/"))


def claim_list(request):
    claims = list(claims_overview())
    ctx = insurance_context(
        request, "claims",
        claims=claims, base=base_currency(),
        open_count=sum(1 for c in claims if c.status in (
            ClaimStatus.OPEN, ClaimStatus.SUBMITTED, ClaimStatus.APPROVED
        )),
    )
    return render(request, "insurance/claim_list.html", ctx)


# --- htmx fragments -------------------------------------------------------------------------

def insurer_search(request):
    q = request.GET.get("q", "").strip()
    orgs = Organization.objects.all()
    if q:
        orgs = orgs.filter(Q(name__icontains=q) | Q(display_name__icontains=q))
    return render(
        request, "insurance/partials/insurer_search.html", {"orgs": orgs[:8], "q": q}
    )
