"""Re-seed the system relationship types so existing tenant schemas gain the `provider_affiliation`
P2O type (Health / Plan D — a doctor's persistent affiliation with a practice, hospital or lab,
mirroring `insured` / `beneficiary` / `cardholder`).

`seed_relationship_types()` is idempotent (`update_or_create` on `code`); brand-new tenants get it
via the `post_schema_sync` receiver instead. Runs once per tenant schema under `migrate_schemas`.
"""

from django.db import migrations


def seed(apps, schema_editor):
    from apps.relationships.seed import seed_relationship_types

    seed_relationship_types()


class Migration(migrations.Migration):
    dependencies = [
        ("relationships", "0007_beneficiary_type"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
