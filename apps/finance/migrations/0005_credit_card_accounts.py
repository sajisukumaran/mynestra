"""Re-seed the chart of accounts for the Cards module (module 4, Part 2):

- `2100 Credit Cards` becomes a group header (`is_postable=False`, `system_key="credit_cards"`) — the
  Cards module nests one postable liability sub-account per real credit card beneath it.
- a new `5860 Interest Expense` account (`system_key="interest_expense"`) for credit-card interest.

`seed_finance()` is idempotent (`update_or_create` on the account code), so it flips the one flag,
sets the system_key, and creates the one new row; brand-new tenants get all of this via the
`post_schema_sync` receiver instead. Runs once per tenant schema under `migrate_schemas`.
"""

from django.db import migrations


def seed(apps, schema_editor):
    from apps.finance.seed import seed_finance

    seed_finance()


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0004_postingmap"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
