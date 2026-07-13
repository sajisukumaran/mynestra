"""Idempotent per-tenant seeding for Payables catalogs (DESIGN §6 idiom).

Mirrors `setup/seed.py` / `finance/seed.py`: loop a catalog constant, `update_or_create` on the
natural key with `is_system=True`. Runs both from the `post_schema_sync` receiver (new tenants) and
from a backfill data migration (existing tenants). Must run inside the tenant schema.
"""

from decimal import Decimal

from apps.payables.models import PaymentTerm

# (name, kind, net_days, discount_percent, discount_days)
PAYMENT_TERMS = [
    ("Due on receipt", PaymentTerm.Kind.DUE_ON_RECEIPT, 0, "0", 0),
    ("Net 15", PaymentTerm.Kind.NET_DAYS, 15, "0", 0),
    ("Net 30", PaymentTerm.Kind.NET_DAYS, 30, "0", 0),
    ("Net 60", PaymentTerm.Kind.NET_DAYS, 60, "0", 0),
    ("2/10 Net 30", PaymentTerm.Kind.NET_DAYS, 30, "2", 10),
]


def seed_payment_terms() -> None:
    for name, kind, net_days, discount_percent, discount_days in PAYMENT_TERMS:
        PaymentTerm.objects.update_or_create(
            name=name,
            defaults={
                "kind": kind,
                "net_days": net_days,
                "discount_percent": Decimal(discount_percent),
                "discount_days": discount_days,
                "is_system": True,
            },
        )
