"""Re-seed the chart of accounts for the Investments module (module 5):

- `1210 Brokerage` / `1220 Retirement` become group headers (`is_postable=False`) and `1200`/`1210`/
  `1220` gain `system_key`s; a new `1230 HSA` header is added — the Investments module nests one
  postable asset sub-account per real investment account beneath the header matching its registration.
- `4300 Investment Income` becomes a group header with postable children `4310 Dividend Income`,
  `4320 Realized Capital Gain/Loss`, `4330 Capital Gains Distributions`, `4340 Investment Interest`.
- a new `5870 Investment Fees` expense account (advisory/account fees; commissions are capitalized).

`seed_finance()` is idempotent (`update_or_create` on the account code), so it flips the flags, sets
the system_keys, and creates the new rows; brand-new tenants get all of this via the
`post_schema_sync` receiver instead. Runs once per tenant schema under `migrate_schemas`.
"""

from django.db import migrations


def seed(apps, schema_editor):
    from apps.finance.seed import seed_finance

    seed_finance()


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0005_credit_card_accounts"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
