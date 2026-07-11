"""Add the `4150 Employer Match` revenue account (system_key `employer_match`) so an employer
retirement-plan match, categorized on a Contribution, reports as compensation income distinct from
salary. `seed_finance()` is idempotent (`update_or_create` on account code), so it just creates the
new row in existing tenant schemas; brand-new tenants get it via the `post_schema_sync` receiver.
Runs once per schema under `migrate_schemas`.
"""

from django.db import migrations


def seed(apps, schema_editor):
    from apps.finance.seed import seed_finance

    seed_finance()


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0006_investment_accounts"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
