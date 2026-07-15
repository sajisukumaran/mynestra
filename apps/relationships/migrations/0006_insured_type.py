"""Re-seed the system relationship types so existing tenant schemas gain the `insured` P2O type
(used by the Automobile module to link a vehicle's drivers to the insurer organization, mirroring
`account_holder` / `cardholder` / `borrower`). The dealer link reuses the existing `customer` type.

`seed_relationship_types()` is idempotent (`update_or_create` on `code`); brand-new tenants get it
via the `post_schema_sync` receiver instead. Runs once per tenant schema under `migrate_schemas`.
"""

from django.db import migrations


def seed(apps, schema_editor):
    from apps.relationships.seed import seed_relationship_types

    seed_relationship_types()


class Migration(migrations.Migration):
    dependencies = [
        ("relationships", "0005_borrower_type"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
