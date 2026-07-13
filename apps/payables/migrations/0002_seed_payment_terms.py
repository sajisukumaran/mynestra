"""Seed the system payment terms (Due on receipt, Net 15/30/60, 2/10 Net 30) into existing tenant
schemas. `seed_payment_terms()` is idempotent (`update_or_create` on name), so it just inserts the
rows; brand-new tenants get them via the `post_schema_sync` receiver instead. Runs once per tenant
schema under `migrate_schemas`.
"""

from django.db import migrations


def seed(apps, schema_editor):
    from apps.payables.seed import seed_payment_terms

    seed_payment_terms()


class Migration(migrations.Migration):
    dependencies = [
        ("payables", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
