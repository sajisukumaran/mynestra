"""Add the `1140 Certificates of Deposit` asset group header (system_key `certificates_of_deposit`)
so bank CDs (term deposits) roll up to their own net-worth line, mirroring `1120 Checking` /
`1130 Savings`. The Banking module nests one postable sub-account per CD beneath it. `seed_finance()`
is idempotent (`update_or_create` on account code), so it just creates the new header in existing
tenant schemas; brand-new tenants get it via the `post_schema_sync` receiver. Runs once per schema
under `migrate_schemas`.
"""

from django.db import migrations


def seed(apps, schema_editor):
    from apps.finance.seed import seed_finance

    seed_finance()


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0007_employer_match"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
