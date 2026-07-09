"""Idempotent per-tenant seeding for the finance backbone (DESIGN §6 idiom).

Mirrors `setup/seed.py` / `relationships/seed.py`: loop a catalog constant, `update_or_create` on
the natural key with `is_system=True` in defaults. Runs both from the `post_schema_sync` receiver
(new tenants) and from a backfill data migration (existing tenants). Must run inside the tenant
schema. Fiscal years/periods are NOT seeded — the service auto-creates them on the first post.
"""

from apps.finance.coa import CHART_OF_ACCOUNTS, CURRENCIES
from apps.finance.models import Account, Currency, default_side_for


def seed_currencies() -> None:
    for code, name, symbol, decimal_places in CURRENCIES:
        Currency.objects.update_or_create(
            code=code,
            defaults={
                "name": name,
                "symbol": symbol,
                "decimal_places": decimal_places,
                "is_system": True,
            },
        )


def seed_chart_of_accounts() -> None:
    by_code: dict[str, Account] = {}
    for code, name, acct_type, parent_code, is_postable, system_key in CHART_OF_ACCOUNTS:
        account, _ = Account.objects.update_or_create(
            code=code,
            defaults={
                "name": name,
                "type": acct_type,
                "normal_side": default_side_for(acct_type),
                "parent": by_code.get(parent_code),
                "is_postable": is_postable,
                "system_key": system_key,
                "is_system": True,
            },
        )
        by_code[code] = account


def seed_finance() -> None:
    seed_currencies()
    seed_chart_of_accounts()
