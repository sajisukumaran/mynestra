"""Re-seed the chart of accounts so existing tenant schemas pick up the Banking-era changes:

- `1120 Checking Account` / `1130 Savings Account` become group headers (`is_postable=False`) — the
  Banking module nests one postable sub-account per real bank account beneath them.
- a new `5850 Bank Charges` expense account (`system_key="bank_charges"`) for fees/charges.

`seed_finance()` is idempotent (`update_or_create` on the account code), so it simply flips the two
`is_postable` flags and creates the one new row; brand-new tenants get all of this via the
`post_schema_sync` receiver instead. Runs once per tenant schema under `migrate_schemas`.
"""

from django.db import migrations


def seed(apps, schema_editor):
    from apps.finance.seed import seed_finance

    seed_finance()


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0002_seed_finance_data"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
