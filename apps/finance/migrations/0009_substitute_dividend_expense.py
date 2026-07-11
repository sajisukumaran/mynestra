"""Add the `5880 Substitute Dividend Expense` account (system_key `substitute_dividend_expense`) so
payments-in-lieu of dividends to a share lender, made while carrying a short position, land on their
own P&L line — distinct from interest expense (5860) and advisory/account fees (5870).
`seed_finance()` is idempotent (`update_or_create` on account code), so it just creates the new
account in existing tenant schemas; brand-new tenants get it via the `post_schema_sync` receiver.
Runs once per schema under `migrate_schemas`.
"""

from django.db import migrations


def seed(apps, schema_editor):
    from apps.finance.seed import seed_finance

    seed_finance()


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0008_certificates_of_deposit"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
