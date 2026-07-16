"""Health service layer — the only sanctioned GL path for the Health module.

An encounter posts nothing. Each provider invoice routes through Payables as a locked bill (+ zero
or more locked partial payments), exactly like the Insurance module's premiums: a `ProviderInvoice`
materializes a `payables.Bill` (`is_locked=True`, `source=<invoice>`, one expense line per EOB
charge — or a single `amount_due` line — to the encounter-type expense account under the 5400
header) via `payables.services.post_bill`; payments are locked `payables.Payment`s allocated to that
bill. The vendor on both is the invoice's biller.

The one novel cross-module piece is HSA funding: a payment funded from an HSA settles Accounts
Payable straight from the health-savings account (an Investments WITHDRAWAL, `contra=AP`), the
direct analog of the Payables LOAN branch — no cash leg, the bill still flips PAID via allocations.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from django.db import transaction

from apps.finance.services import (
    LineInput,
    base_currency,
    post_entry,
    resolve_account,
    resolve_posting_account,
)
from apps.health.models import (
    ENCOUNTER_TYPE_ACCOUNT,
    OWED_STATUSES,
    Encounter,
    EncounterType,
    Funding,
    InvoiceStatus,
    Prescription,
    ProviderInvoice,
    VisitStatus,
)

ZERO = Decimal("0")

# The single Expert-mode remappable activity per invoice (the medical-expense home). The default is
# resolved from the encounter type; Expert users can remap per invoice via a PostingMap.
POSTING_ACTIVITIES = [
    {"key": "expense", "label": "Medical expense", "kind": "expense", "default": "medical_expense"},
]


# --- Expense home resolution -----------------------------------------------------------------

def _invoice_encounter_type(inv: ProviderInvoice) -> str:
    return inv.encounter.encounter_type if inv.encounter_id else EncounterType.MEDICAL


def _expense_account(inv: ProviderInvoice):
    """The GL account a provider invoice's bill line posts to — the encounter-type default (under
    the 5400 header), overridable per invoice in Expert mode via a PostingMap."""
    etype = _invoice_encounter_type(inv)
    default = ENCOUNTER_TYPE_ACCOUNT.get(etype, "medical_expense")
    return resolve_posting_account(inv, "expense", default)


# --- Vendor tagging (reuse the Payables catalog) ---------------------------------------------

def _ensure_vendor_category(org) -> None:
    from apps.setup.models import Category

    cat = Category.objects.filter(kind=Category.Kind.ORG, name="Vendor").first()
    if cat:
        org.categories.add(cat)


def _ensure_vendor_profile(inv: ProviderInvoice) -> None:
    from apps.payables.services import ensure_vendor_profile

    if inv.biller_organization_id:
        _ensure_vendor_category(inv.biller_organization)
        ensure_vendor_profile(organization=inv.biller_organization)
    elif inv.biller_person_id:
        ensure_vendor_profile(person=inv.biller_person)


# --- Locked bill sync ------------------------------------------------------------------------

def _invoice_ct():
    from django.contrib.contenttypes.models import ContentType

    return ContentType.objects.get_for_model(ProviderInvoice)


def _recompute_amount_due(inv: ProviderInvoice) -> None:
    """When EOB charges are present, `amount_due` = Σ their patient responsibility (else the user's
    bare total is kept)."""
    charges = list(inv.charges.all())
    if not charges:
        return
    total = sum((c.patient_responsibility for c in charges), ZERO)
    if inv.amount_due != total:
        inv.amount_due = total
        inv.save(update_fields=["amount_due", "updated_at"])


def _single_line_desc(inv: ProviderInvoice) -> str:
    return inv.memo or f"{inv.biller_name} — statement"


def _sync_bill(inv: ProviderInvoice, *, user=None):
    """Create (or repost in place) the locked Payables bill backing this invoice — one expense line
    per EOB charge (else a single `amount_due` line) to the encounter-type account, tagged as
    sourced from this invoice. A written-off invoice collapses to a single adjusted-total line."""
    from apps.payables.models import Bill, BillLine
    from apps.payables.services import post_bill, repost_bill

    bill = inv.bill or Bill(is_locked=True)
    bill.vendor_person = inv.biller_person
    bill.vendor_organization = inv.biller_organization
    bill.bill_date = inv.invoice_date
    bill.due_date = inv.due_date or inv.invoice_date
    bill.currency = base_currency()
    bill.vendor_ref = inv.invoice_number or inv.reference
    bill.notes = inv.memo
    bill.is_locked = True
    bill.source_content_type = _invoice_ct()
    bill.source_object_id = inv.pk
    bill.save()

    bill.lines.all().delete()  # rewritten each save
    account = _expense_account(inv)
    charges = [c for c in inv.charges.all() if c.patient_responsibility > ZERO]
    use_charges = bool(charges) and inv.status != InvoiceStatus.WRITTEN_OFF
    if use_charges:
        for order, ch in enumerate(charges):
            BillLine.objects.create(
                bill=bill, line_type=BillLine.LineType.EXPENSE, order=order,
                description=ch.description or "Service", account=account,
                quantity=Decimal("1"), unit_price=ch.patient_responsibility,
            )
    else:
        BillLine.objects.create(
            bill=bill, line_type=BillLine.LineType.EXPENSE, order=0,
            description=_single_line_desc(inv), account=account,
            quantity=Decimal("1"), unit_price=inv.amount_due,
        )

    if inv.bill_id is None:
        post_bill(bill, user=user)
        inv.bill = bill
        inv.save(update_fields=["bill", "updated_at"])
    else:
        repost_bill(bill, user=user)
    return bill


# --- Status / rollup derivation --------------------------------------------------------------

def _allocated(inv: ProviderInvoice) -> Decimal:
    """Total allocated against this invoice's locked bill (drives its paid status)."""
    if inv.bill_id is None:
        return ZERO
    from apps.payables.models import PaymentAllocation

    return sum(
        (a.amount for a in PaymentAllocation.objects.filter(bill_id=inv.bill_id)), ZERO
    )


def _recompute_invoice_status(inv: ProviderInvoice) -> None:
    """Derive UNPAID / PARTIALLY_PAID / PAID / OVERPAID from the bill's allocations (and refunds
    already received). PENDING_INSURANCE / DISPUTED / WRITTEN_OFF are sticky manual states."""
    if inv.status in (
        InvoiceStatus.PENDING_INSURANCE, InvoiceStatus.DISPUTED, InvoiceStatus.WRITTEN_OFF
    ):
        return
    paid = _allocated(inv)
    total = inv.amount_due
    refund_owed = ZERO
    if paid <= ZERO:
        status = InvoiceStatus.UNPAID
    elif paid < total:
        status = InvoiceStatus.PARTIALLY_PAID
    elif paid == total:
        status = InvoiceStatus.PAID
    else:  # overpaid — net of refunds already received
        refund_owed = paid - total - (inv.refunded or ZERO)
        if refund_owed > ZERO:
            status = InvoiceStatus.OVERPAID
        else:
            status, refund_owed = InvoiceStatus.PAID, ZERO
    changed = []
    if inv.status != status:
        inv.status = status
        changed.append("status")
    if inv.refund_expected != refund_owed:
        inv.refund_expected = refund_owed
        changed.append("refund_expected")
    if changed:
        inv.save(update_fields=[*changed, "updated_at"])


def _invoice_billed(inv: ProviderInvoice) -> Decimal:
    """The provider's gross charge (Σ EOB `billed` if itemized, else the patient total)."""
    charges = list(inv.charges.all())
    if charges:
        return sum((c.billed or ZERO for c in charges), ZERO)
    return inv.amount_due


def _recompute_encounter(enc: Encounter | None) -> None:
    """Recompute an encounter's denormalized rollups from its invoices (a pure read cache)."""
    if enc is None:
        return
    invoices = list(enc.invoices.all())
    billed = sum((_invoice_billed(inv) for inv in invoices), ZERO)
    responsibility = sum((inv.amount_due for inv in invoices), ZERO)
    paid = sum((_allocated(inv) for inv in invoices), ZERO)
    outstanding = sum(
        (inv.amount_due - _allocated(inv) for inv in invoices if inv.status in OWED_STATUSES), ZERO
    )
    fields = {
        "total_billed": billed,
        "total_patient_responsibility": responsibility,
        "total_paid": paid,
        "total_outstanding": outstanding if outstanding > ZERO else ZERO,
    }
    dirty = [k for k, v in fields.items() if getattr(enc, k) != v]
    if dirty:
        for k in dirty:
            setattr(enc, k, fields[k])
        enc.save(update_fields=[*dirty, "updated_at"])


# --- Insurance plan linkage (P2) -------------------------------------------------------------

# EncounterType → the insurance PolicyType a plan must be to cover it.
def _encounter_to_policy_type():
    from apps.insurance.models import PolicyType

    return {
        EncounterType.MEDICAL: PolicyType.HEALTH,
        EncounterType.HOSPITAL: PolicyType.HEALTH,
        EncounterType.DENTAL: PolicyType.DENTAL,
        EncounterType.VISION: PolicyType.VISION,
    }


def active_health_plan(patient, *, on_date=None, encounter_type=None):
    """The active insurance policy (carrying a HealthPlan) that covers `patient` for the given
    `encounter_type` on `on_date` — used to auto-link an encounter. Prefers the type-matching
    policy; returns None if none applies. A pure read."""
    from apps.insurance.models import COVERED_ROLES, InsurancePolicy, PolicyStatus

    on_date = on_date or datetime.date.today()
    qs = InsurancePolicy.objects.filter(
        status=PolicyStatus.ACTIVE,
        health_plan__isnull=False,
        members__person=patient,
        members__role__in=list(COVERED_ROLES),
    ).distinct()
    ptype = _encounter_to_policy_type().get(encounter_type) if encounter_type else None
    if ptype is not None:
        qs = qs.filter(policy_type=ptype)
    for policy in qs.order_by("-effective_date", "-id"):
        if policy.effective_date and policy.effective_date > on_date:
            continue
        if policy.expiry_date and policy.expiry_date < on_date:
            continue
        return policy
    return None


def _meter(label: str, used, limit, *, kind="deductible", person=None) -> dict:
    """A single accumulator meter row for the deductible / OOP display."""
    used = used or ZERO
    limit = limit or ZERO
    pct = int(min(Decimal("100"), (used / limit * 100))) if limit > ZERO else 0
    return {
        "label": label, "kind": kind, "person": person,
        "used": used, "limit": limit, "pct": pct,
        "remaining": (limit - used) if limit > used else ZERO,
        "met": limit > ZERO and used >= limit,
    }


def deductible_oop_status(policy, *, as_of=None) -> dict | None:
    """The deductible / out-of-pocket accumulator for an insurance policy's HealthPlan (twin of
    `investments.contribution_limit_status`). Sums each covered person's EOB charges over the plan-
    year window: `deductible_used` = Σ deductible_amount (applies_to_deductible); `oop_used` =
    Σ (deductible + copay + coinsurance) (applies_to_oop). Family = the sum across covered persons;
    an individual counts as met when their OWN or the FAMILY aggregate reaches its limit
    (individual-embedded-in-family). Dental annual-max / vision allowance = Σ insurance_paid vs the
    benefit cap. Returns a `{plan, window, meters, family, persons}` bundle, or None with no
    HealthPlan. Posts nothing — a pure read."""
    hp = getattr(policy, "health_plan", None)
    if hp is None:
        return None
    start, end = hp.plan_year_window(as_of)
    from apps.health.models import InvoiceCharge

    def _charges(person=None):
        qs = InvoiceCharge.objects.filter(
            invoice__encounter__plan=policy,
            invoice__invoice_date__gte=start,
            invoice__invoice_date__lte=end,
        )
        if person is not None:
            qs = qs.filter(invoice__encounter__patient=person)
        return qs

    from apps.insurance.models import COVERED_ROLES

    covered = list(
        policy.members.filter(role__in=list(COVERED_ROLES)).select_related("person")
    )
    seen, persons = set(), []
    fam_ded_used = fam_oop_used = ZERO
    for m in covered:
        if m.person_id in seen:
            continue
        seen.add(m.person_id)
        ded = oop = ZERO
        for c in _charges(m.person):
            if c.applies_to_deductible:
                ded += c.deductible_amount or ZERO
            if c.applies_to_oop:
                oop += (c.deductible_amount or ZERO) + (c.copay_amount or ZERO) \
                    + (c.coinsurance_amount or ZERO)
        fam_ded_used += ded
        fam_oop_used += oop
        persons.append({"person": m.person, "deductible_used": ded, "oop_used": oop})

    fam_ded_limit = hp.deductible_family or ZERO
    fam_oop_limit = hp.oop_max_family or ZERO
    ind_ded_limit = hp.deductible_individual or ZERO
    ind_oop_limit = hp.oop_max_individual or ZERO

    # Individual-embedded-in-family: an individual limit also counts as met once the FAMILY
    # aggregate reaches its cap, even if that person's own spend hasn't.
    fam_ded_met = fam_ded_limit > ZERO and fam_ded_used >= fam_ded_limit
    fam_oop_met = fam_oop_limit > ZERO and fam_oop_used >= fam_oop_limit

    meters = []
    if fam_ded_limit > ZERO:
        meters.append(_meter("Family deductible", fam_ded_used, fam_ded_limit))
    if fam_oop_limit > ZERO:
        meters.append(_meter("Family out-of-pocket max", fam_oop_used, fam_oop_limit, kind="oop"))
    for row in persons:
        p = row["person"]
        name = getattr(p, "display_name", "") or str(p)
        if ind_ded_limit > ZERO:
            mt = _meter(f"{name} — deductible", row["deductible_used"], ind_ded_limit, person=p)
            mt["met"] = mt["met"] or fam_ded_met
            meters.append(mt)
        if ind_oop_limit > ZERO:
            mt = _meter(f"{name} — out-of-pocket", row["oop_used"], ind_oop_limit,
                        kind="oop", person=p)
            mt["met"] = mt["met"] or fam_oop_met
            meters.append(mt)

    # Benefit caps (dental annual max / vision allowance) — Σ what the plan PAID this year.
    paid_total = sum((c.insurance_paid or ZERO for c in _charges()), ZERO)
    dental = vision = None
    if hp.dental_annual_max:
        dental = _meter("Dental benefit used", paid_total, hp.dental_annual_max, kind="benefit")
        meters.append(dental)
    if hp.vision_allowance:
        vision = _meter("Vision allowance used", paid_total, hp.vision_allowance, kind="benefit")
        meters.append(vision)

    return {
        "plan": hp, "policy": policy, "window": (start, end), "meters": meters,
        "persons": persons, "dental": dental, "vision": vision,
        "family": {
            "deductible_used": fam_ded_used, "deductible_limit": fam_ded_limit,
            "oop_used": fam_oop_used, "oop_limit": fam_oop_limit,
        },
    }


# --- Encounter orchestration -----------------------------------------------------------------

@transaction.atomic
def save_encounter(enc: Encounter, *, user=None, is_new=True):
    """Persist a visit and refresh its rollups. The caller has saved the encounter row (so it has a
    pk). Auto-links the visit to the patient's active health plan (P2) when one isn't set, then
    recomputes the denormalized totals from whatever invoices it currently groups."""
    if enc.plan_id is None and enc.patient_id:
        plan = active_health_plan(
            enc.patient, on_date=enc.date, encounter_type=enc.encounter_type
        )
        if plan is not None:
            enc.plan = plan
            enc.save(update_fields=["plan", "updated_at"])
    _recompute_encounter(enc)
    return enc


# --- Duplicate detection ---------------------------------------------------------------------

def duplicate_warnings(inv: ProviderInvoice) -> list[dict]:
    """Warn (not block) when this invoice looks like a duplicate of an existing one: same biller +
    invoice number, or same biller + amount + invoice date. Returns a list of {invoice, reason}."""
    if inv.biller is None:
        return []
    base = ProviderInvoice.objects.exclude(pk=inv.pk or 0)
    base = (
        base.filter(biller_person=inv.biller_person)
        if inv.biller_person_id
        else base.filter(biller_organization=inv.biller_organization)
    )
    warnings, seen = [], set()
    if inv.invoice_number:
        for other in base.filter(invoice_number=inv.invoice_number):
            if other.pk not in seen:
                warnings.append({"invoice": other, "reason": "same biller and invoice number"})
                seen.add(other.pk)
    for other in base.filter(amount_due=inv.amount_due, invoice_date=inv.invoice_date):
        if other.pk not in seen:
            warnings.append({"invoice": other, "reason": "same biller, amount and date"})
            seen.add(other.pk)
    return warnings


# --- Invoice orchestration -------------------------------------------------------------------

def _teardown_bill(inv: ProviderInvoice, *, user=None) -> None:
    """Remove an invoice's locked bill (and the module's own payments), refusing if a FOREIGN
    payment is allocated. Used when an invoice returns to Pending-insurance or is deleted."""
    from apps.payables.services import delete_bill

    bill = inv.bill
    module_pks = set(_module_payments(inv).values_list("pk", flat=True))
    _teardown_module_payments(inv, user=user)
    if bill is not None:
        foreign = bill.allocations.exclude(payment_id__in=module_pks).exists()
        if foreign:
            raise ValueError(
                "A Payables payment is allocated to this bill — remove it there first."
            )
        inv.bill = None
        inv.save(update_fields=["bill", "updated_at"])
        delete_bill(bill, user=user)
        bill.hard_delete()


@transaction.atomic
def save_invoice(inv: ProviderInvoice, *, user=None, is_new=True) -> list[dict]:
    """Persist a provider invoice and sync its locked bill. Posts the bill only when the invoice has
    left Pending-insurance (and has a biller); Pending-insurance posts nothing. Recomputes
    amount_due from any EOB charges, the invoice status, and the encounter rollups. Returns
    duplicate warnings (informational — never blocks). The caller has saved the row (so it has a
    pk)."""
    _recompute_amount_due(inv)
    warnings = duplicate_warnings(inv)

    if inv.status == InvoiceStatus.PENDING_INSURANCE:
        # Not on the books yet — tear down any prior accrual (only if no foreign payment).
        if inv.bill_id is not None:
            _teardown_bill(inv, user=user)
        _recompute_encounter(inv.encounter)
        return warnings

    if inv.amount_due <= ZERO:
        # Nothing for the patient to pay (e.g. a fully insurance-covered service) — no bill to
        # accrue, but the invoice + its EOB charges are kept (they feed deductible/OOP tracking).
        if inv.bill_id is not None:
            _teardown_bill(inv, user=user)
        if inv.status not in (
            InvoiceStatus.DISPUTED, InvoiceStatus.WRITTEN_OFF, InvoiceStatus.PAID
        ):
            inv.status = InvoiceStatus.PAID
            inv.save(update_fields=["status", "updated_at"])
        _recompute_encounter(inv.encounter)
        return warnings

    if inv.biller is None:
        raise ValueError("Set the invoice's biller before posting it.")
    _ensure_vendor_profile(inv)
    _sync_bill(inv, user=user)
    _recompute_invoice_status(inv)
    _recompute_encounter(inv.encounter)
    return warnings


@transaction.atomic
def confirm_invoice(inv: ProviderInvoice, *, user=None) -> ProviderInvoice:
    """Confirm a Pending-insurance invoice (the amount is now final): flip it to Unpaid and post its
    bill. The caller has already applied the confirmed amount / charges to the row."""
    inv.status = InvoiceStatus.UNPAID
    inv.save(update_fields=["status", "updated_at"])
    save_invoice(inv, user=user, is_new=False)
    return inv


# --- Payments (partial-payment friendly) -----------------------------------------------------

def _module_payments(inv: ProviderInvoice):
    from apps.payables.models import Payment

    return Payment.objects.filter(source_content_type=_invoice_ct(), source_object_id=inv.pk)


def invoice_payments(inv: ProviderInvoice):
    """The locked payments recorded against an invoice (exact content-type scoped), oldest first."""
    return _module_payments(inv).order_by("date", "id")


def _teardown_module_payments(inv: ProviderInvoice, *, user=None) -> None:
    from apps.payables.services import delete_payment

    for pay in list(_module_payments(inv)):
        delete_payment(pay, user=user)
        pay.hard_delete()


def _payment_funding_kind(funding: str):
    from apps.payables.models import Payment

    return {
        Funding.BANK: Payment.Funding.BANK,
        Funding.CARD: Payment.Funding.CARD,
        Funding.CASH: Payment.Funding.CASH,
        Funding.HSA: Payment.Funding.HSA,
    }[funding]


def _apply_payment_funding(pay, funding, *, account=None, card=None, cash=None, hsa=None) -> None:
    pay.funding_kind = _payment_funding_kind(funding)
    pay.bank_account = account if funding == Funding.BANK else None
    pay.credit_card = card if funding == Funding.CARD else None
    pay.cash_account = cash if funding == Funding.CASH else None
    pay.hsa_account = hsa if funding == Funding.HSA else None


def _persist_last_funding(inv, funding, *, account=None, card=None, cash=None, hsa=None) -> None:
    inv.funding_source = funding
    inv.funding_account = account if funding == Funding.BANK else None
    inv.credit_card = card if funding == Funding.CARD else None
    inv.cash_account = cash if funding == Funding.CASH else None
    inv.hsa_account = hsa if funding == Funding.HSA else None
    inv.save(update_fields=[
        "funding_source", "funding_account", "credit_card", "cash_account", "hsa_account",
        "updated_at",
    ])


@transaction.atomic
def record_invoice_payment(
    inv: ProviderInvoice, *, amount, date, funding,
    account=None, card=None, cash=None, hsa=None, user=None,
):
    """Record one locked payment against a confirmed invoice, allocated in full to its bill.
    Repeated calls make partial payments (a payment plan); the bill's PAID / PARTIALLY_PAID status
    derives from the allocation totals. Funding is bank / card / cash / HSA (HSA settles AP straight
    from the health-savings account, no cash leg). Returns the created payment."""
    from apps.payables.models import Payment
    from apps.payables.services import apply_payment

    if inv.bill_id is None:
        raise ValueError("Confirm the invoice before recording a payment.")
    if funding == Funding.HSA and hsa is None:
        raise ValueError("Choose the HSA to pay from.")
    pay = Payment(
        vendor_person=inv.biller_person, vendor_organization=inv.biller_organization,
        date=date, amount=amount, is_locked=True,
        source_content_type=_invoice_ct(), source_object_id=inv.pk,
        reference=inv.reference,
    )
    _apply_payment_funding(pay, funding, account=account, card=card, cash=cash, hsa=hsa)
    pay.save()
    apply_payment(pay, [(inv.bill, amount)], user=user)
    _persist_last_funding(inv, funding, account=account, card=card, cash=cash, hsa=hsa)
    _recompute_invoice_status(inv)
    _recompute_encounter(inv.encounter)
    return pay


@transaction.atomic
def delete_invoice_payment(inv: ProviderInvoice, payment, *, user=None) -> None:
    """Tear down one payment (reverse its HSA / bank / card / cash leg, reopen the bill) and refresh
    the invoice + encounter."""
    from apps.payables.services import delete_payment

    delete_payment(payment, user=user)
    payment.hard_delete()
    _recompute_invoice_status(inv)
    _recompute_encounter(inv.encounter)


@transaction.atomic
def record_visit_copay(
    enc: Encounter, *, amount, funding,
    account=None, card=None, cash=None, hsa=None, user=None,
):
    """Quick-capture a copay paid at the time of a visit: a locked ProviderInvoice under the
    encounter (biller = its facility, else its primary provider) carrying a single copay charge
    (feeds the out-of-pocket accumulator, not the deductible), posted and paid in full from the
    chosen funding source. Returns the invoice, or None when the amount isn't positive. Reuses the
    normal invoice bill + payment seams, so the copay lands in the encounter-type expense account,
    the provider ledger, and OOP tracking (via the encounter's linked plan)."""
    from apps.health.models import InvoiceCharge

    amount = Decimal(amount)
    if amount <= ZERO:
        return None
    biller_org = enc.facility
    biller_person = enc.primary_provider if biller_org is None else None
    if biller_org is None and biller_person is None:
        raise ValueError("Add a facility or primary provider to record a copay.")
    inv = ProviderInvoice(
        encounter=enc, biller_organization=biller_org, biller_person=biller_person,
        invoice_date=enc.date, status=InvoiceStatus.UNPAID, memo="Copay at visit",
    )
    inv.save()
    InvoiceCharge.objects.create(
        invoice=inv, description="Copay", copay_amount=amount,
        applies_to_deductible=False, applies_to_oop=True, order=0,
    )
    save_invoice(inv, user=user, is_new=True)
    record_invoice_payment(
        inv, amount=amount, date=enc.date, funding=funding,
        account=account, card=card, cash=cash, hsa=hsa, user=user,
    )
    return inv


# --- Write-off / dispute / refund ------------------------------------------------------------

@transaction.atomic
def write_off_invoice(inv: ProviderInvoice, *, new_total=ZERO, user=None) -> ProviderInvoice:
    """Adjust an invoice down to a confirmed lower total (write-off / adjustment). Reposts its bill
    in place at the new total (expense + AP drop); a full write-off with nothing paid unposts the
    bill entirely. Refuses to go below what's already been paid."""
    new_total = new_total if new_total is not None else ZERO
    paid = _allocated(inv)
    if new_total < paid:
        raise ValueError("The adjusted total can't be less than what's already been paid.")
    inv.amount_due = new_total
    inv.status = InvoiceStatus.WRITTEN_OFF
    inv.save(update_fields=["amount_due", "status", "updated_at"])
    if inv.bill_id is not None:
        if new_total <= ZERO and paid <= ZERO:
            from apps.payables.services import unpost_bill

            unpost_bill(inv.bill, user=user)
        else:
            _sync_bill(inv, user=user)  # single adjusted-total line, reposted in place
    _recompute_encounter(inv.encounter)
    return inv


@transaction.atomic
def dispute_invoice(inv: ProviderInvoice, *, user=None) -> ProviderInvoice:
    """Flag an invoice Disputed — it stays accrued (no GL change) but drops out of the you-owe /
    overdue totals until resolved."""
    inv.status = InvoiceStatus.DISPUTED
    inv.save(update_fields=["status", "updated_at"])
    _recompute_encounter(inv.encounter)
    return inv


@transaction.atomic
def resolve_dispute(inv: ProviderInvoice, *, user=None) -> ProviderInvoice:
    """Clear the Disputed flag — the status returns to whatever the allocations imply."""
    inv.status = InvoiceStatus.UNPAID  # provisional; _recompute derives the real status
    inv.save(update_fields=["status", "updated_at"])
    _recompute_invoice_status(inv)
    _recompute_encounter(inv.encounter)
    return inv


@transaction.atomic
def record_refund(
    inv: ProviderInvoice, *, amount, dest, date, bank=None, cash=None, hsa=None, user=None
):
    """Clear (part of) an overpayment credit: bring money back into `dest` and debit it out of
    Accounts Payable (the biller owes you). Bank routes through a native banking DEPOSIT categorized
    to AP (register-truthful); cash posts a direct entry `DR cash / CR AP`; HSA posts an Investments
    CONTRIBUTION with `contra=AP` (funds returning to the HSA). Accumulates into `refunded` and
    re-derives the status."""
    amount = Decimal(amount)
    if amount <= ZERO:
        raise ValueError("A refund amount must be positive.")
    ap = resolve_account("accounts_payable")
    party = {"person": inv.biller_person, "organization": inv.biller_organization}

    if dest == Funding.HSA:
        if hsa is None:
            raise ValueError("Choose the HSA the refund returns to.")
        from apps.investments.services import record_hsa_return

        record_hsa_return(
            hsa, amount=amount, date=date, contra=ap,
            payee_person=inv.biller_person, payee_organization=inv.biller_organization,
            memo=f"Refund from {inv.biller_name}", user=user,
        )
    elif dest == Funding.BANK:
        if bank is None:
            raise ValueError("Choose the bank account the refund lands in.")
        from apps.banking.models import BankTransaction
        from apps.banking.models import TxnType as BankTxnType
        from apps.banking.services import post_transaction as bank_post

        leg = BankTransaction.objects.create(
            account=bank, txn_type=BankTxnType.DEPOSIT, date=date, amount=amount,
            category_account=ap,
            payee_person=inv.biller_person, payee_organization=inv.biller_organization,
            counter_external=f"Refund from {inv.biller_name}",
        )
        bank_post(leg, user=user)
    else:  # cash
        cash_leg = cash or resolve_account("1110")
        cur = base_currency()
        post_entry(
            date=date,
            lines=[
                LineInput(cash_leg, debit=amount, currency=cur),
                LineInput(ap, credit=amount, currency=cur, **party),
            ],
            description=f"{inv.biller_name}: medical refund",
            memo=inv.memo,
            user=user,
        )

    inv.refunded = (inv.refunded or ZERO) + amount
    inv.save(update_fields=["refunded", "updated_at"])
    _recompute_invoice_status(inv)
    _recompute_encounter(inv.encounter)
    return inv


@transaction.atomic
def delete_invoice(inv: ProviderInvoice, *, user=None) -> None:
    """Hard-erase an invoice: delete the module's own payments first, refuse if a FOREIGN payment is
    allocated to the bill, then erase the bill + entry + the invoice. Refreshes the encounter."""
    enc = inv.encounter
    _teardown_bill(inv, user=user)  # tears down module payments, refuses on a foreign allocation
    inv.hard_delete()
    _recompute_encounter(enc)


# --- Prescriptions (P3 — a med fill; same locked-bill / partial-payment seams) ---------------

def _prescription_ct():
    from django.contrib.contenttypes.models import ContentType

    return ContentType.objects.get_for_model(Prescription)


def _prescription_expense_account(rx: Prescription):
    """A prescription fill always books to Pharmacy / Prescriptions (5440), Expert-remappable."""
    return resolve_posting_account(rx, "expense", "pharmacy_expense")


def _ensure_pharmacy_vendor(rx: Prescription) -> None:
    from apps.payables.services import ensure_vendor_profile

    if rx.pharmacy_organization_id:
        _ensure_vendor_category(rx.pharmacy_organization)
        ensure_vendor_profile(organization=rx.pharmacy_organization)


def _sync_prescription_bill(rx: Prescription, *, user=None):
    """Create (or repost in place) the locked Payables bill backing this fill — a single expense
    line for the fill cost to Pharmacy / Prescriptions, sourced from this prescription."""
    from apps.payables.models import Bill, BillLine
    from apps.payables.services import post_bill, repost_bill

    bill = rx.bill or Bill(is_locked=True)
    bill.vendor_organization = rx.pharmacy_organization
    bill.vendor_person = None
    bill.bill_date = rx.date
    bill.due_date = rx.date
    bill.currency = base_currency()
    bill.vendor_ref = rx.reference
    bill.notes = rx.memo
    bill.is_locked = True
    bill.source_content_type = _prescription_ct()
    bill.source_object_id = rx.pk
    bill.save()

    bill.lines.all().delete()  # rewritten each save
    BillLine.objects.create(
        bill=bill, line_type=BillLine.LineType.EXPENSE, order=0,
        description=rx.memo or f"{rx.drug_name} — fill",
        account=_prescription_expense_account(rx),
        quantity=Decimal("1"), unit_price=rx.cost,
    )

    if rx.bill_id is None:
        post_bill(bill, user=user)
        rx.bill = bill
        rx.save(update_fields=["bill", "updated_at"])
    else:
        repost_bill(bill, user=user)
    return bill


def _recompute_prescription_status(rx: Prescription) -> None:
    """Derive UNPAID / PARTIALLY_PAID / PAID / OVERPAID from the bill's allocations (net of any
    refunds received) — the ProviderInvoice derivation, keyed on `cost`."""
    paid = _allocated(rx)
    total = rx.cost
    refund_owed = ZERO
    if paid <= ZERO:
        status = InvoiceStatus.UNPAID
    elif paid < total:
        status = InvoiceStatus.PARTIALLY_PAID
    elif paid == total:
        status = InvoiceStatus.PAID
    else:
        refund_owed = paid - total - (rx.refunded or ZERO)
        status = InvoiceStatus.OVERPAID if refund_owed > ZERO else InvoiceStatus.PAID
        refund_owed = refund_owed if refund_owed > ZERO else ZERO
    changed = []
    if rx.status != status:
        rx.status = status
        changed.append("status")
    if rx.refund_expected != refund_owed:
        rx.refund_expected = refund_owed
        changed.append("refund_expected")
    if changed:
        rx.save(update_fields=[*changed, "updated_at"])


def _teardown_prescription_bill(rx: Prescription, *, user=None) -> None:
    """Remove a prescription's locked bill (+ the module's own payments), refusing on a FOREIGN
    payment allocation."""
    from apps.payables.services import delete_bill

    bill = rx.bill
    module_pks = set(_module_rx_payments(rx).values_list("pk", flat=True))
    _teardown_rx_payments(rx, user=user)
    if bill is not None:
        foreign = bill.allocations.exclude(payment_id__in=module_pks).exists()
        if foreign:
            raise ValueError(
                "A Payables payment is allocated to this bill — remove it there first."
            )
        rx.bill = None
        rx.save(update_fields=["bill", "updated_at"])
        delete_bill(bill, user=user)
        bill.hard_delete()


@transaction.atomic
def save_prescription(rx: Prescription, *, user=None, is_new=True):
    """Persist a prescription and sync its locked bill. Posts the bill when the fill has a positive
    cost and a pharmacy; a zero-cost fill (e.g. fully covered) accrues nothing but keeps the record
    + refill tracking. The caller has saved the row (so it has a pk)."""
    if rx.cost <= ZERO:
        if rx.bill_id is not None:
            _teardown_prescription_bill(rx, user=user)
        if rx.status != InvoiceStatus.PAID:
            rx.status = InvoiceStatus.PAID
            rx.save(update_fields=["status", "updated_at"])
        return rx
    if rx.pharmacy_organization_id is None:
        raise ValueError("Choose the pharmacy before posting the fill.")
    _ensure_pharmacy_vendor(rx)
    _sync_prescription_bill(rx, user=user)
    _recompute_prescription_status(rx)
    return rx


def _module_rx_payments(rx: Prescription):
    from apps.payables.models import Payment

    return Payment.objects.filter(
        source_content_type=_prescription_ct(), source_object_id=rx.pk
    )


def prescription_payments(rx: Prescription):
    """The locked payments recorded against a prescription (content-type scoped), oldest first."""
    return _module_rx_payments(rx).order_by("date", "id")


def _teardown_rx_payments(rx: Prescription, *, user=None) -> None:
    from apps.payables.services import delete_payment

    for pay in list(_module_rx_payments(rx)):
        delete_payment(pay, user=user)
        pay.hard_delete()


@transaction.atomic
def record_prescription_payment(
    rx: Prescription, *, amount, date, funding,
    account=None, card=None, cash=None, hsa=None, user=None,
):
    """Record one locked payment against a posted prescription, allocated in full to its bill.
    Repeated calls make partial payments; funding is bank / card / cash / HSA (HSA settles AP
    straight from the health-savings account). Returns the created payment."""
    from apps.payables.models import Payment
    from apps.payables.services import apply_payment

    if rx.bill_id is None:
        raise ValueError("Post the prescription (a positive cost) before recording a payment.")
    if funding == Funding.HSA and hsa is None:
        raise ValueError("Choose the HSA to pay from.")
    pay = Payment(
        vendor_organization=rx.pharmacy_organization, date=date, amount=amount, is_locked=True,
        source_content_type=_prescription_ct(), source_object_id=rx.pk, reference=rx.reference,
    )
    _apply_payment_funding(pay, funding, account=account, card=card, cash=cash, hsa=hsa)
    pay.save()
    apply_payment(pay, [(rx.bill, amount)], user=user)
    _persist_last_funding(rx, funding, account=account, card=card, cash=cash, hsa=hsa)
    _recompute_prescription_status(rx)
    return pay


@transaction.atomic
def delete_prescription_payment(rx: Prescription, payment, *, user=None) -> None:
    """Tear down one prescription payment (reverse its HSA / bank / card leg, reopen the bill)."""
    from apps.payables.services import delete_payment

    delete_payment(payment, user=user)
    payment.hard_delete()
    _recompute_prescription_status(rx)


@transaction.atomic
def delete_prescription(rx: Prescription, *, user=None) -> None:
    """Hard-erase a prescription: delete its own payments, refuse on a FOREIGN allocation, then
    erase the bill + entry + the prescription."""
    _teardown_prescription_bill(rx, user=user)
    rx.hard_delete()


# --- Provider affiliation (persistent doctor ↔ business P2O link) -----------------------------

def link_provider_affiliation(person, organization) -> None:
    """Persist a `provider_affiliation` P2O link (doctor ↔ practice / hospital / lab). Add-only;
    no-ops when the type isn't seeded or either side is missing."""
    if person is None or organization is None:
        return
    from apps.relationships.models import PersonOrgRelationship, PersonOrgRelationshipType

    rel_type = PersonOrgRelationshipType.objects.filter(code="provider_affiliation").first()
    if rel_type is None:
        return
    PersonOrgRelationship.objects.get_or_create(
        person=person, organization=organization, type=rel_type
    )


def provider_affiliations(person):
    """Organizations a doctor is affiliated with (drives the picker's practice suggestions)."""
    from apps.relationships.models import PersonOrgRelationship

    return PersonOrgRelationship.objects.filter(
        person=person, type__code="provider_affiliation"
    ).select_related("organization")


# --- Documents -------------------------------------------------------------------------------

def delete_document(doc) -> None:
    """Remove a health document — delete the stored file, then the row (a plain attachment)."""
    if doc.file:
        doc.file.delete(save=False)
    doc.delete()


# --- Read models (pure; post nothing) --------------------------------------------------------

def _owed_invoices():
    """Invoices that count as owed now (unpaid / partially paid), with their bill preloaded."""
    return ProviderInvoice.objects.filter(status__in=list(OWED_STATUSES)).select_related(
        "bill", "biller_person", "biller_organization", "encounter"
    )


def outstanding_by_provider() -> list[dict]:
    """Outstanding balance grouped by biller, largest first — the core 'who do I still owe' view."""
    groups: dict = {}
    for inv in _owed_invoices():
        biller = inv.biller
        key = (inv.biller_kind, biller.pk if biller else 0)
        row = groups.setdefault(
            key,
            {
                "biller": biller,
                "name": inv.biller_name or "—",
                "kind": inv.biller_kind,
                "outstanding": ZERO,
                "count": 0,
                "overdue": False,
            },
        )
        row["outstanding"] += inv.outstanding
        row["count"] += 1
        row["overdue"] = row["overdue"] or inv.is_overdue
    rows = [r for r in groups.values() if r["outstanding"] > ZERO]
    rows.sort(key=lambda r: r["outstanding"], reverse=True)
    return rows


def total_unpaid() -> Decimal:
    """Total you currently owe across all providers (unpaid + partially paid, excludes disputed)."""
    return sum((inv.outstanding for inv in _owed_invoices()), ZERO)


def overdue_invoices():
    """Owed invoices past their due date, soonest-due first — drives the overdue reminder card."""
    today = datetime.date.today()
    rows = [inv for inv in _owed_invoices() if inv.due_date and inv.due_date < today]
    rows.sort(key=lambda inv: inv.due_date)
    return rows


def recent_payments(limit: int = 8):
    """The household's most recent Health-sourced payments (dashboard 'recently paid')."""
    from apps.payables.models import Payment

    return list(
        Payment.objects.filter(source_content_type=_invoice_ct())
        .select_related("vendor_person", "vendor_organization")
        .order_by("-date", "-id")[:limit]
    )


def dashboard_stats() -> dict:
    """Headline figures for the Health dashboard."""
    encounters = Encounter.objects.count()
    owed = list(_owed_invoices())
    total_owed = sum((inv.outstanding for inv in owed), ZERO)
    overdue = [inv for inv in owed if inv.is_overdue]
    overdue_total = sum((inv.outstanding for inv in overdue), ZERO)
    return {
        "encounters_count": encounters,
        "invoices_count": ProviderInvoice.objects.count(),
        "total_owed": total_owed,
        "owed_count": len(owed),
        "overdue_total": overdue_total,
        "overdue_count": len(overdue),
        "by_provider": outstanding_by_provider(),
        "recent_payments": recent_payments(),
    }


def active_health_plans():
    """Active insurance policies that carry a HealthPlan (medical / dental / vision) — the
    household's live benefit plans. Drives the OOP meters + the plan-year reset reminder."""
    from apps.insurance.models import InsurancePolicy, PolicyStatus

    return list(
        InsurancePolicy.objects.filter(
            status=PolicyStatus.ACTIVE, health_plan__isnull=False
        ).select_related("health_plan", "insurer_organization", "insurer_person")
    )


def active_health_insurance() -> list[dict]:
    """Every active health-related insurance policy (medical / dental / vision), whether or not a
    HealthPlan cost-sharing satellite is set — so the dashboard surfaces the household's coverage
    even before deductible/OOP figures are entered. Each row carries the covered members and the
    deductible/OOP status (None without a HealthPlan). A pure read."""
    from apps.insurance.models import COVERED_ROLES, InsurancePolicy, PolicyStatus, PolicyType

    policies = (
        InsurancePolicy.objects.filter(
            status=PolicyStatus.ACTIVE,
            policy_type__in=[PolicyType.HEALTH, PolicyType.DENTAL, PolicyType.VISION],
        )
        .select_related("insurer_organization", "insurer_person")
        .prefetch_related("members__person")
        .order_by("policy_type", "-effective_date", "-id")
    )
    rows = []
    for p in policies:
        covered = [
            m.person for m in p.members.all() if m.role in COVERED_ROLES and m.person_id
        ]
        rows.append({"policy": p, "status": deductible_oop_status(p), "covered": covered})
    return rows


# Prescription / refill glyph — a placeholder from the current sprite (the UI-gate sprite regen
# swaps in a real `pill` mark, as with the encounter-type glyphs).
PRESCRIPTION_GLYPH = "heart"


def reminders_due(within_days: int = 90) -> list[dict]:
    """One soonest-first health reminders feed (twin of `automobile.services.renewals_due`), pulled
    from four sources within the horizon: unpaid / partially-paid invoices with a due date; upcoming
    scheduled appointments; prescription refills coming due (while refills remain); and each active
    plan's deductible / plan-year reset. Rows are `{record, kind, label, glyph, tint, date, days,
    url}`. A pure read — posts nothing."""
    today = datetime.date.today()
    horizon = today + datetime.timedelta(days=within_days)
    rows = []

    for inv in ProviderInvoice.objects.filter(
        status__in=list(OWED_STATUSES), due_date__isnull=False, due_date__lte=horizon
    ).select_related("biller_person", "biller_organization", "bill"):
        rows.append({
            "record": inv, "kind": "invoice",
            "label": f"Invoice due · {inv.biller_name}",
            "glyph": "banknote", "tint": "rose",
            "date": inv.due_date, "days": (inv.due_date - today).days,
            "url": f"health/invoices/{inv.pk}/",
        })

    for enc in Encounter.objects.filter(
        visit_status=VisitStatus.SCHEDULED, date__lte=horizon
    ).select_related("patient", "facility"):
        rows.append({
            "record": enc, "kind": "appointment",
            "label": f"{enc.type_label} appointment · {enc.patient_name}",
            "glyph": "calendar-days", "tint": enc.type_tint,
            "date": enc.date, "days": (enc.date - today).days,
            "url": f"health/visits/{enc.pk}/",
        })

    for rx in Prescription.objects.filter(
        next_refill_date__isnull=False, next_refill_date__lte=horizon, refills_remaining__gt=0
    ).select_related("pharmacy_organization", "patient"):
        rows.append({
            "record": rx, "kind": "refill",
            "label": f"Refill · {rx.display}",
            "glyph": PRESCRIPTION_GLYPH, "tint": "teal",
            "date": rx.next_refill_date, "days": (rx.next_refill_date - today).days,
            "url": f"health/prescriptions/{rx.pk}/",
        })

    for policy in active_health_plans():
        hp = getattr(policy, "health_plan", None)
        if hp is None:
            continue
        _, end = hp.plan_year_window(today)
        reset = end + datetime.timedelta(days=1)
        if reset <= horizon:
            rows.append({
                "record": policy, "kind": "plan_year",
                "label": f"Deductible resets · {policy.display}",
                "glyph": "shield-check", "tint": "violet",
                "date": reset, "days": (reset - today).days,
                "url": f"health/plans/{policy.pk}/edit/",
            })

    rows.sort(key=lambda r: r["date"])
    return rows


def hsa_summary() -> dict:
    """HSA balances + this-year contribution room, read from Investments (registration = HSA). The
    balance is the GL node balance (cash for a cash-only HSA); contribution eligibility comes from
    `investments.contribution_limit_status`. Returns `{rows, total, count}`; empty when there's no
    HSA. A pure read."""
    from apps.investments.models import InvestmentAccount, Registration
    from apps.investments.services import contribution_limit_status

    accts = list(
        InvestmentAccount.objects.filter(registration=Registration.HSA, is_active=True)
        .select_related("gl_account", "institution")
    )
    this_year = datetime.date.today().year
    rows, total = [], ZERO
    for a in accts:
        bal = a.balance
        total += bal
        status = contribution_limit_status(a)
        limit_row = None
        if status:
            limit_row = next((r for r in status["rows"] if r["year"] == this_year), None)
        rows.append({
            "account": a, "balance": bal,
            "coverage": a.get_hsa_coverage_display() if a.hsa_coverage else "",
            "limit_row": limit_row,
        })
    return {"rows": rows, "total": total, "count": len(rows)}


def _deductible_by_person() -> dict:
    """person_pk → an individual-deductible meter, aggregated from every active health plan."""
    out = {}
    for policy in active_health_plans():
        status = deductible_oop_status(policy)
        if not status:
            continue
        limit = status["plan"].deductible_individual or ZERO
        if limit <= ZERO:
            continue
        for pr in status["persons"]:
            out[pr["person"].pk] = _meter(
                "Deductible", pr["deductible_used"], limit, person=pr["person"]
            )
    return out


def member_rollups() -> list[dict]:
    """Per-patient health rollups — billed / responsibility / paid / outstanding across a person's
    encounters, with their individual-deductible meter when a plan covers them. Largest outstanding
    first. A pure read (aggregates the encounter denorm caches)."""
    from django.db.models import Count, Sum

    from apps.contacts.models import Person

    agg = list(
        Encounter.objects.values("patient")
        .annotate(
            billed=Sum("total_billed"),
            responsibility=Sum("total_patient_responsibility"),
            paid=Sum("total_paid"),
            outstanding=Sum("total_outstanding"),
            visits=Count("id"),
        )
        .order_by("-outstanding", "-billed")
    )
    ded = _deductible_by_person()
    people = {p.pk: p for p in Person.objects.filter(pk__in=[a["patient"] for a in agg])}
    rows = []
    for a in agg:
        person = people.get(a["patient"])
        if person is None:
            continue
        rows.append({
            "person": person,
            "billed": a["billed"] or ZERO,
            "responsibility": a["responsibility"] or ZERO,
            "paid": a["paid"] or ZERO,
            "outstanding": a["outstanding"] or ZERO,
            "visits": a["visits"],
            "deductible": ded.get(person.pk),
        })
    return rows


def launcher_counts() -> list[dict]:
    """Live counts for the launcher tile: encounters / invoices / amount you owe."""
    return [
        {"n": Encounter.objects.count(), "label": "Visits"},
        {"n": ProviderInvoice.objects.count(), "label": "Invoices"},
        {"n": total_unpaid(), "label": "You owe"},
    ]
