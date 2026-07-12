"""Add the Payables control/support accounts and give AP a stable role handle:
`2300` renamed **Accounts Payable** (system_key `accounts_payable`), plus `1430 Household Goods &
Equipment` (`household_goods`, for capitalized warranty-tracked purchases), `4920 Purchase
Discounts` (`purchase_discounts`, early-payment/discount lines), `5920 Shipping & Delivery`
(`shipping_expense`) and `5930 Sales Tax` (`sales_tax_paid`). `seed_finance()` is idempotent
(`update_or_create` on account code), so it renames/keys `2300` and creates the new accounts in
existing tenant schemas; brand-new tenants get them via the `post_schema_sync` receiver. Runs once
per schema under `migrate_schemas`.
"""

from django.db import migrations


def seed(apps, schema_editor):
    from apps.finance.seed import seed_finance

    seed_finance()


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0009_substitute_dividend_expense"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
