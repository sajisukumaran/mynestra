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

from apps.finance.models import ZERO
from apps.finance.services import base_currency, resolve_posting_account
from apps.insurance.models import (
    COVERED_ROLES,
    Funding,
    InsurancePolicy,
    InsurancePremium,
    PolicyAsset,
    PolicyStatus,
    PolicyType,
)

# PolicyType → the Standard-mode default premium expense account (a stable system_key). Auto keeps
# its own 5340 (continuity with the Automobile module); home/renters use the escrow / renters homes;
# the rest fold into the per-type children under 5500 (5510/5520/5530), else 5590 Other Insurance.
POLICY_TYPE_ACCOUNT = {
    PolicyType.AUTO: "vehicle_insurance",       # 5340
    PolicyType.HOME: "home_insurance",          # 5150
    PolicyType.RENTERS: "renters_insurance",    # 5540
    PolicyType.HEALTH: "health_insurance",      # 5510
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


# --- Covered-person ("insured" P2O) synchronisation ------------------------------------------

def sync_policy_p2o(policy: InsurancePolicy, *, user=None) -> None:
    """Ensure each covered member (policyholder / insured / dependent / driver) has an org-level
    'insured' P2O link to the insurer org. Add-only; no-ops when the type isn't seeded / no insurer
    org is set. (Beneficiary P2O is Phase 3 — the `beneficiary` type isn't seeded yet.)"""
    if not policy.insurer_organization_id:
        return
    from apps.relationships.models import PersonOrgRelationship, PersonOrgRelationshipType

    insured = PersonOrgRelationshipType.objects.filter(code="insured").first()
    if insured is None:
        return
    for m in policy.members.filter(role__in=list(COVERED_ROLES)).select_related("person"):
        PersonOrgRelationship.objects.get_or_create(
            person=m.person, organization=policy.insurer_organization, type=insured
        )


# --- Covered-asset links (Vehicle now; Real Estate in Plan C) --------------------------------

def _vehicle_ct():
    from django.contrib.contenttypes.models import ContentType

    from apps.automobile.models import Vehicle

    return ContentType.objects.get_for_model(Vehicle)


def set_covered_vehicles(policy: InsurancePolicy, vehicles) -> None:
    """Replace the policy's covered-Vehicle assets with `vehicles` (used by auto policies)."""
    ct = _vehicle_ct()
    keep = set()
    for v in vehicles:
        asset, _ = PolicyAsset.objects.get_or_create(
            policy=policy, content_type=ct, object_id=v.pk
        )
        keep.add(asset.pk)
    policy.assets.filter(content_type=ct).exclude(pk__in=keep).delete()


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
    }


def launcher_counts() -> list[dict]:
    """Live counts for the launcher tile: active policies / annual premium / renewals due soon."""
    due = policies_expiring(within_days=45)
    return [
        {"n": _active_policies().count(), "label": "Active policies"},
        {"n": annual_premium_total(), "label": "Annual premium"},
        {"n": len(due), "label": "Renewals due"},
    ]
