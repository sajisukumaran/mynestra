"""Re-seed system categories so existing tenant schemas pick up the new 'Vendor' Org category
(module 6, Payables) — a bill's vendor Organization is tagged with this locked system category, the
way Banking uses 'Bank' and Investments uses 'Brokerage'.

`seed_categories()` is idempotent (`update_or_create` on kind + name), so it simply adds the one new
row; brand-new tenants get it via the `post_schema_sync` receiver instead. Runs once per tenant
schema under `migrate_schemas`.
"""

from django.db import migrations


def seed(apps, schema_editor):
    from apps.setup.seed import seed_categories

    seed_categories()


class Migration(migrations.Migration):
    dependencies = [
        ("setup", "0002_brokerage_category"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
