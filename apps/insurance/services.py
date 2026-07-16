"""Insurance service layer — the only sanctioned GL path for the Insurance module.

A policy posts nothing. Premiums route through Payables as locked bills (+ optional locked
payments), exactly like the Automobile module's cost events: an `InsurancePremium` materializes a
`payables.Bill` (`is_locked=True`, `source=<premium>`, one line to the policy-type expense account)
via `payables.services.post_bill`; when funded, a locked `payables.Payment` (BANK/CARD/CASH)
allocated to the bill via `apply_payment`. The vendor on both is the policy's insurer.

Claims (Phase 2) post a direct finance entry; not implemented here yet.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from django.db import transaction

from apps.finance.models import ZERO, JournalEntry
from apps.finance.services import (
    LineInput,
    base_currency,
    post_entry,
    resolve_posting_account,
    reverse_entry,
)
from apps.insurance.models import (
    COVERED_ROLES,
    OPEN_CLAIM_STATUSES,
    Claim,
    Funding,
    InsurancePolicy,
    InsurancePremium,
    MemberRole,
    PayoutDestination,
    PolicyAsset,
    PolicyStatus,
    PolicyType,
)

# Fixed structural legs for claim payouts (resolved by stable system_key / code — never remappable).
TRANSFER_CLEARING = "transfer_clearing"  # 1150 — routes bank money-in via a native TRANSFER_IN
CASH_ON_HAND = "1110"

# PolicyType → the Standard-mode default premium expense account (a stable system_key). Auto keeps
# its own 5340 (continuity with the Automobile module); home/renters use the escrow / renters homes;
# the rest fold into the per-type children under 5500 (5510/5520/5530), else 5590 Other Insurance.
POLICY_TYPE_ACCOUNT = {
    PolicyType.AUTO: "vehicle_insurance",       # 5340
    PolicyType.HOME: "home_insurance",          # 5150
    PolicyType.RENTERS: "renters_insurance",    # 5540
    PolicyType.HEALTH: "health_insurance",      # 5510
    PolicyType.DENTAL: "dental_insurance",      # 5550
    PolicyType.VISION: "vision_insurance",      # 5560
    PolicyType.LIFE: "life_insurance",          # 5520
    PolicyType.UMBRELLA: "umbrella_insurance",  # 5530
    PolicyType.DISABILITY: "other_insurance",   # 5590
    PolicyType.PET: "other_insurance",          # 5590
    PolicyType.OTHER: "other_insurance",        # 5590
}

# The single Expert-mode remappable activity per policy (the premium expense home).
POSTING_ACTIVITIES = [
    {"key": "premium", "label": "Premium", "kind": "expense", "default": "other_insurance"},
]


def _premium_account(policy: InsurancePolicy):
    """The GL account a policy's premium bill line posts to — the per-type default, overridable per
    policy in Expert mode via a PostingMap."""
    default = POLICY_TYPE_ACCOUNT.get(policy.policy_type, "other_insurance")
    return resolve_posting_account(policy, "premium", default)


# --- Vendor tagging (reuse the Payables catalog) ---------------------------------------------

def _ensure_vendor_category(org) -> None:
    from apps.setup.models import Category

    cat = Category.objects.filter(kind=Category.Kind.ORG, name="Vendor").first()
    if cat:
        org.categories.add(cat)


def _ensure_vendor_profile(policy: InsurancePolicy) -> None:
    from apps.payables.services import ensure_vendor_profile

    if policy.insurer_organization_id:
        _ensure_vendor_category(policy.insurer_organization)
        ensure_vendor_profile(organization=policy.insurer_organization)
    elif policy.insurer_person_id:
        ensure_vendor_profile(person=policy.insurer_person)


# --- Locked bill + payment sync --------------------------------------------------------------

def _premium_ct():
    from django.contrib.contenttypes.models import ContentType

    return ContentType.objects.get_for_model(InsurancePremium)


def _bill_line_desc(premium: InsurancePremium) -> str:
    if premium.memo:
        return premium.memo
    return f"{premium.policy.display} — premium"


def _sync_bill(premium: InsurancePremium, *, user=None):
    """Create (or repost in place) the locked Payables bill backing this premium — one line to the
    policy-type expense account, tagged as sourced from this premium."""
    from apps.payables.models import Bill, BillLine
    from apps.payables.services import post_bill, repost_bill

    policy = premium.policy
    bill = premium.bill or Bill(is_locked=True)
    bill.vendor_person = policy.insurer_person
    bill.vendor_organization = policy.insurer_organization
    bill.bill_date = premium.date
    bill.due_date = premium.due_date or premium.date
    bill.currency = policy.currency or base_currency()
    bill.vendor_ref = premium.reference
    bill.notes = premium.memo
    bill.is_locked = True
    bill.source_content_type = _premium_ct()
    bill.source_object_id = premium.pk
    bill.save()

    bill.lines.all().delete()  # single-line, rewritten each save
    BillLine.objects.create(
        bill=bill, line_type=BillLine.LineType.EXPENSE, order=0,
        description=_bill_line_desc(premium), account=_premium_account(policy),
        quantity=Decimal("1"), unit_price=premium.amount,
    )

    if premium.bill_id is None:
        post_bill(bill, user=user)
        premium.bill = bill
        premium.save(update_fields=["bill", "updated_at"])
    else:
        repost_bill(bill, user=user)
    return bill


def _payment_funding(source: str):
    from apps.payables.models import Payment

    return {
        Funding.BANK: Payment.Funding.BANK,
        Funding.CARD: Payment.Funding.CARD,
        Funding.CASH: Payment.Funding.CASH,
    }[source]


def _module_payments(premium: InsurancePremium):
    from apps.payables.models import Payment

    return Payment.objects.filter(
        source_content_type=_premium_ct(), source_object_id=premium.pk
    )


def _teardown_module_payments(premium: InsurancePremium, *, user=None):
    from apps.payables.services import delete_payment

    for pay in list(_module_payments(premium)):
        delete_payment(pay, user=user)
        pay.hard_delete()
    if premium.payment_id is not None:
        premium.payment = None
        premium.save(update_fields=["payment", "updated_at"])


def _new_locked_payment(premium: InsurancePremium):
    from apps.payables.models import Payment

    policy = premium.policy
    return Payment(
        vendor_person=policy.insurer_person,
        vendor_organization=policy.insurer_organization,
        date=premium.date,
        is_locked=True,
        source_content_type=_premium_ct(),
        source_object_id=premium.pk,
    )


def _sync_single_payment(premium: InsurancePremium, *, user=None):
    """Create / repost / remove the ONE locked funding payment for a premium, allocated in full to
    its bill. NONE funding leaves the bill accrued (unpaid)."""
    from apps.payables.services import apply_payment, repost_payment

    policy = premium.policy
    bill = premium.bill
    if not premium.is_funded:
        _teardown_module_payments(premium, user=user)
        return None

    pay = premium.payment or _new_locked_payment(premium)
    pay.vendor_person = policy.insurer_person
    pay.vendor_organization = policy.insurer_organization
    pay.date = premium.date
    pay.amount = premium.amount
    pay.funding_kind = _payment_funding(premium.funding_source)
    pay.bank_account = premium.funding_account if premium.funding_source == Funding.BANK else None
    pay.credit_card = premium.credit_card if premium.funding_source == Funding.CARD else None
    pay.cash_account = premium.cash_account if premium.funding_source == Funding.CASH else None
    pay.is_locked = True
    pay.source_content_type = _premium_ct()
    pay.source_object_id = premium.pk

    if premium.payment_id is None:
        pay.save()
        apply_payment(pay, [(bill, premium.amount)], user=user)
        premium.payment = pay
        premium.save(update_fields=["payment", "updated_at"])
    else:
        pay.save()
        repost_payment(pay, [(bill, premium.amount)], user=user)
    return pay


def _recompute_policy_expiry(policy: InsurancePolicy, *, latest: InsurancePremium | None = None):
    """Advance the policy's renewal date from a premium's `covers_through` (mirrors the Automobile
    module's `covers_through` → renewal seam)."""
    if latest is not None and latest.covers_through:
        if policy.expiry_date != latest.covers_through:
            policy.expiry_date = latest.covers_through
            policy.save(update_fields=["expiry_date", "updated_at"])


# --- Premium orchestration -------------------------------------------------------------------

@transaction.atomic
def save_premium(premium: InsurancePremium, *, user=None, is_new=True):
    """Post a saved premium: tag the insurer vendor, build the locked bill and (if funded) the
    locked payment, then advance the policy's renewal date. The caller has saved the premium row
    (so it has a pk). Requires the policy to have an insurer (the bill's single vendor)."""
    policy = premium.policy
    if policy.insurer is None:
        raise ValueError("Set the policy's insurer before recording a premium.")
    _ensure_vendor_profile(policy)
    _sync_bill(premium, user=user)
    _sync_single_payment(premium, user=user)
    _recompute_policy_expiry(policy, latest=premium)
    return premium


@transaction.atomic
def delete_premium(premium: InsurancePremium, *, user=None):
    """Hard-erase a premium: delete the module's own payment(s) first, refuse if a FOREIGN payment
    is allocated to the bill, then erase the bill + entry + the premium."""
    from apps.payables.services import delete_bill

    bill = premium.bill
    module_pks = set(_module_payments(premium).values_list("pk", flat=True))
    _teardown_module_payments(premium, user=user)
    if bill is not None:
        foreign = bill.allocations.exclude(payment_id__in=module_pks).exists()
        if foreign:
            raise ValueError(
                "A payment recorded in Payables is allocated to this bill — delete it there first."
            )
        delete_bill(bill, user=user)
        bill.hard_delete()
    premium.hard_delete()


@transaction.atomic
def void_premium(premium: InsurancePremium, *, user=None):
    """Reverse the premium's GL impact but keep the record (Void): unpost its bill + tear down its
    payment(s). Used where a closed period blocks an in-place repost."""
    from apps.payables.services import unpost_bill

    _teardown_module_payments(premium, user=user)
    if premium.bill_id is not None:
        unpost_bill(premium.bill, user=user)


# --- Claims (Phase 2) ------------------------------------------------------------------------
# Two posting paths, both through existing service layers (never hand-written ledger rows):
#   * REIMBURSEMENT — a direct finance entry crediting the loss expense account (the payout offsets
#     the already-booked loss; net retained expense = the deductible). Bank payout routes via 1150 +
#     a native banking TRANSFER_IN (register-truthful); cash debits the chosen cash account / 1110.
#   * TOTAL_LOSS (auto) — delegates entirely to automobile.post_disposal (method=TOTAL_LOSS,
#     proceeds=payout); the claim posts NOTHING of its own (the disposal owns the 4930 entry).
# Cross-module imports (automobile / banking) stay lazy.

def _claim_key(claim: Claim) -> str:
    return f"insurance:claim:{claim.pk}:v{claim.posting_version}"


def _claim_vehicle(claim: Claim):
    """The covered Vehicle this claim concerns, if its `claimed_asset` is one (else None)."""
    from apps.automobile.models import Vehicle

    asset = claim.claimed_asset
    return asset if isinstance(asset, Vehicle) else None


def _reimbursement_postable(claim: Claim) -> bool:
    """A reimbursement posts only once a payout is recorded, an expense home is chosen, and a valid
    destination is set (an open / denied / no-payout claim posts nothing)."""
    if claim.is_total_loss or not claim.has_payout or claim.loss_expense_account_id is None:
        return False
    if claim.payout_destination == PayoutDestination.BANK:
        return claim.bank_account_id is not None
    return claim.payout_destination == PayoutDestination.CASH


def _reimbursement_lines(claim: Claim) -> list[LineInput]:
    """Dr the payout destination (1150 for a bank leg, else the cash account / 1110), Cr the loss
    expense account. Balanced by construction (two lines, equal amount)."""
    policy = claim.policy
    cur = policy.currency or base_currency()
    amount = claim.payout_amount
    insurer = {"person": policy.insurer_person, "organization": policy.insurer_organization}
    if claim.payout_destination == PayoutDestination.BANK:
        cash_leg = TRANSFER_CLEARING
    else:
        cash_leg = claim.cash_account or CASH_ON_HAND
    return [
        LineInput(cash_leg, debit=amount, currency=cur, **insurer),
        LineInput(claim.loss_expense_account, credit=amount, currency=cur),
    ]


def _sync_claim_bank_leg(claim: Claim, *, user=None):
    """For a payout to a tracked bank account, post a native banking TRANSFER_IN (Dr bank gl / Cr
    1150) so 1150 nets to zero and the register shows the deposit (the disposal-proceeds idiom)."""
    if claim.payout_destination != PayoutDestination.BANK or claim.bank_account_id is None:
        return None
    from apps.banking.models import BankTransaction
    from apps.banking.models import TxnType as BankTxnType
    from apps.banking.services import post_transaction as bank_post

    leg = BankTransaction.objects.create(
        account=claim.bank_account, txn_type=BankTxnType.TRANSFER_IN,
        date=claim.payout_date or claim.loss_date, amount=claim.payout_amount,
        counter_external=f"{claim.policy.display} claim payout",
    )
    bank_post(leg, user=user)
    claim.bank_txn = leg
    return leg


def _teardown_claim_bank_leg(claim: Claim, *, user=None):
    if claim.bank_txn_id is None:
        return
    from apps.banking.services import delete_transaction

    leg = claim.bank_txn
    claim.bank_txn = None
    claim.save(update_fields=["bank_txn", "updated_at"])
    delete_transaction(leg, user=user)


def _post_reimbursement(claim: Claim, *, user=None):
    if not _reimbursement_postable(claim):
        return
    entry = post_entry(
        date=claim.payout_date or claim.loss_date,
        lines=_reimbursement_lines(claim),
        source=claim,
        external_key=_claim_key(claim),
        description=f"{claim.policy.display}: claim payout",
        memo=claim.notes,
        user=user,
    )
    claim.journal_entry = entry
    _sync_claim_bank_leg(claim, user=user)
    claim.save(update_fields=["journal_entry", "bank_txn", "updated_at"])


def _unpost_reimbursement(claim: Claim, *, user=None):
    """Reverse a reimbursement's entry + tear down its bank leg (safe when nothing was posted)."""
    _teardown_claim_bank_leg(claim, user=user)
    entry = claim.journal_entry
    if entry is not None and entry.status == JournalEntry.Status.POSTED:
        reverse_entry(entry, user=user)
    if claim.journal_entry_id is not None:
        claim.journal_entry = None
        claim.save(update_fields=["journal_entry", "updated_at"])


def _disposal_proceeds_account(claim: Claim):
    return claim.bank_account if claim.payout_destination == PayoutDestination.BANK else None


def _post_total_loss(claim: Claim, *, user=None):
    """Create (or repost, on edit) the auto write-off disposal. The disposal owns the 4930 entry;
    the claim only links to it. Requires a covered Vehicle with no pre-existing disposal."""
    from apps.automobile.models import DisposalMethod, VehicleDisposal
    from apps.automobile.services import post_disposal, repost_disposal

    vehicle = _claim_vehicle(claim)
    if vehicle is None:
        raise ValueError("Select the covered vehicle for a total-loss claim.")
    policy = claim.policy
    date = claim.payout_date or claim.loss_date

    if claim.disposal_id is None:
        if hasattr(vehicle, "disposal"):
            raise ValueError("This vehicle already has a disposal recorded — edit that instead.")
        disposal = VehicleDisposal(
            vehicle=vehicle, method=DisposalMethod.TOTAL_LOSS, date=date,
            proceeds=claim.payout_amount or ZERO,
            proceeds_account=_disposal_proceeds_account(claim),
            buyer_person=policy.insurer_person, buyer_organization=policy.insurer_organization,
            notes=claim.notes,
        )
        disposal.save()
        post_disposal(disposal, user=user)
        claim.disposal = disposal
        claim.save(update_fields=["disposal", "updated_at"])
    else:
        disposal = claim.disposal
        disposal.method = DisposalMethod.TOTAL_LOSS
        disposal.date = date
        disposal.proceeds = claim.payout_amount or ZERO
        disposal.proceeds_account = _disposal_proceeds_account(claim)
        disposal.buyer_person = policy.insurer_person
        disposal.buyer_organization = policy.insurer_organization
        disposal.notes = claim.notes
        disposal.save()
        repost_disposal(disposal, user=user)


@transaction.atomic
def save_claim(claim: Claim, *, user=None, is_new=True):
    """Post a saved claim down its settlement path (the caller has saved the row, so it has a pk).
    On edit (`is_new=False`), the reimbursement path reverses-and-rebuilds (version bump); the
    total-loss path reposts its disposal in place. `settlement_kind` is fixed after creation."""
    if claim.is_total_loss:
        _post_total_loss(claim, user=user)
        return claim
    if not is_new:
        _unpost_reimbursement(claim, user=user)
        claim.posting_version += 1
        claim.save(update_fields=["posting_version", "updated_at"])
    _post_reimbursement(claim, user=user)
    return claim


@transaction.atomic
def void_claim(claim: Claim, *, user=None):
    """Reverse the claim's GL impact but keep the record (Void). Total loss → unpost the disposal
    (restores the vehicle to active); reimbursement → reverse the entry + tear down the bank leg."""
    if claim.is_total_loss:
        if claim.disposal_id is not None:
            from apps.automobile.services import unpost_disposal

            unpost_disposal(claim.disposal, user=user)
    else:
        _unpost_reimbursement(claim, user=user)


@transaction.atomic
def delete_claim(claim: Claim, *, user=None):
    """Hard-erase the claim and everything it created. Total loss → delete the disposal (its entry,
    legs; restores the vehicle); reimbursement → erase the entry + bank leg."""
    if claim.is_total_loss:
        if claim.disposal_id is not None:
            from apps.automobile.services import delete_disposal

            disposal = claim.disposal
            claim.disposal = None
            claim.save(update_fields=["disposal", "updated_at"])
            delete_disposal(disposal, user=user)
    else:
        _teardown_claim_bank_leg(claim, user=user)
        entry = claim.journal_entry
        if entry is not None:
            entry.hard_delete()
    claim.hard_delete()


def set_claim_vehicle(claim: Claim, vehicle) -> None:
    """Point a claim's `claimed_asset` GFK at a Vehicle (used for auto claims / total loss)."""
    if vehicle is None:
        claim.content_type = None
        claim.object_id = None
        return
    from django.contrib.contenttypes.models import ContentType

    claim.content_type = ContentType.objects.get_for_model(vehicle.__class__)
    claim.object_id = vehicle.pk


# --- Documents (Phase 3) ---------------------------------------------------------------------

def delete_document(doc) -> None:
    """Remove a policy document — delete the stored file, then the row (a plain attachment)."""
    if doc.file:
        doc.file.delete(save=False)
    doc.delete()


# --- Covered-person ("insured" P2O) synchronisation ------------------------------------------

def sync_policy_p2o(policy: InsurancePolicy, *, user=None) -> None:
    """Ensure each covered member (policyholder / insured / dependent / driver) has an org-level
    'insured' P2O link to the insurer, and each beneficiary a 'beneficiary' link. Add-only; no-ops
    per type when it isn't seeded / no insurer org is set."""
    if not policy.insurer_organization_id:
        return
    from apps.relationships.models import PersonOrgRelationship, PersonOrgRelationshipType

    types = {
        t.code: t
        for t in PersonOrgRelationshipType.objects.filter(code__in=["insured", "beneficiary"])
    }

    def _link(members, rel_type):
        if rel_type is None:
            return
        for m in members.select_related("person"):
            PersonOrgRelationship.objects.get_or_create(
                person=m.person, organization=policy.insurer_organization, type=rel_type
            )

    _link(policy.members.filter(role__in=list(COVERED_ROLES)), types.get("insured"))
    _link(policy.members.filter(role=MemberRole.BENEFICIARY), types.get("beneficiary"))


# --- Covered-asset links (Vehicle now; Real Estate in Plan C) --------------------------------

def _vehicle_ct():
    from django.contrib.contenttypes.models import ContentType

    from apps.automobile.models import Vehicle

    return ContentType.objects.get_for_model(Vehicle)


def _property_ct():
    from django.contrib.contenttypes.models import ContentType

    from apps.realestate.models import Property

    return ContentType.objects.get_for_model(Property)


def _set_covered_assets(policy: InsurancePolicy, ct, objs) -> None:
    """Replace the policy's covered assets OF ONE content-type with `objs`, leaving assets of every
    other content-type untouched (so a policy can cover a vehicle AND a property without either
    picker clobbering the other)."""
    keep = set()
    for obj in objs:
        asset, _ = PolicyAsset.objects.get_or_create(
            policy=policy, content_type=ct, object_id=obj.pk
        )
        keep.add(asset.pk)
    policy.assets.filter(content_type=ct).exclude(pk__in=keep).delete()


def set_covered_vehicles(policy: InsurancePolicy, vehicles) -> None:
    """Replace the policy's covered-Vehicle assets with `vehicles` (used by auto policies)."""
    _set_covered_assets(policy, _vehicle_ct(), vehicles)


def set_covered_properties(policy: InsurancePolicy, properties) -> None:
    """Replace the policy's covered-Property assets with `properties` (home / renters policies)."""
    _set_covered_assets(policy, _property_ct(), properties)


def policies_for_asset(asset, *, active_only=True):
    """Policies whose covered-asset set includes `asset` (drives the vehicle read-through card)."""
    from django.contrib.contenttypes.models import ContentType

    ct = ContentType.objects.get_for_model(asset.__class__)
    qs = InsurancePolicy.objects.filter(
        assets__content_type=ct, assets__object_id=asset.pk
    ).distinct()
    if active_only:
        qs = qs.filter(status=PolicyStatus.ACTIVE)
    return qs


# --- Read models (pure; post nothing) --------------------------------------------------------

def policies_expiring(within_days: int = 90) -> list[dict]:
    """Active policies whose renewal (expiry) date falls within `within_days`, soonest first (past-
    due-but-active first). A pure read."""
    today = datetime.date.today()
    horizon = today + datetime.timedelta(days=within_days)
    rows = []
    for policy in InsurancePolicy.objects.filter(status=PolicyStatus.ACTIVE).select_related(
        "insurer_organization", "insurer_person"
    ):
        when = policy.expiry_date
        if when is None or when > horizon:
            continue
        rows.append({"policy": policy, "date": when, "days": (when - today).days})
    rows.sort(key=lambda r: r["date"])
    return rows


def _active_policies():
    return InsurancePolicy.objects.filter(status=PolicyStatus.ACTIVE)


def annual_premium_total() -> Decimal:
    return sum((p.annualized_premium for p in _active_policies()), ZERO)


def open_claims():
    """Claims still in progress (open / submitted / approved) — drives the dashboard stat + list."""
    return Claim.objects.filter(status__in=list(OPEN_CLAIM_STATUSES))


def payouts_total() -> Decimal:
    """Lifetime claim payouts received across all policies (dashboard stat)."""
    from django.db.models import Sum

    return Claim.objects.aggregate(t=Sum("payout_amount"))["t"] or ZERO


def claims_overview():
    """All claims across policies, newest loss first — for the global Claims list."""
    return Claim.objects.select_related(
        "policy", "policy__insurer_organization", "policy__insurer_person"
    ).order_by("-loss_date", "-id")


def dashboard_stats() -> dict:
    """Headline figures for the Insurance dashboard."""
    policies = list(
        InsurancePolicy.objects.select_related(
            "insurer_organization", "insurer_person", "currency"
        )
    )
    active = [p for p in policies if p.is_active]
    return {
        "policies": policies,
        "policies_count": len(policies),
        "active_count": len(active),
        "annual_premium": sum((p.annualized_premium for p in active), ZERO),
        "renewals": policies_expiring(within_days=90),
        "open_claims_count": open_claims().count(),
        "recent_claims": list(claims_overview()[:6]),
        "payouts_total": payouts_total(),
    }


def launcher_counts() -> list[dict]:
    """Live counts for the launcher tile: active policies / annual premium / open claims."""
    return [
        {"n": _active_policies().count(), "label": "Active policies"},
        {"n": annual_premium_total(), "label": "Annual premium"},
        {"n": open_claims().count(), "label": "Open claims"},
    ]
