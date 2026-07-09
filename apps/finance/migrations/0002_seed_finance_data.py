"""Seed the finance catalogs (currencies + chart of accounts) into every tenant schema.

The `post_schema_sync` receiver only fires when a schema is first created, so tenants that existed
before the finance app (e.g. the demo household) need this data migration. django-tenants'
`migrate_schemas` runs this once per tenant schema with the connection pinned to that schema, and
finance is a TENANT_APP (never runs on `public`). `seed_finance()` is idempotent (update_or_create),
so re-running it — including for brand-new tenants that also hit the signal path — is a no-op.
"""

from django.db import migrations


def seed(apps, schema_editor):
    from apps.finance.seed import seed_finance

    seed_finance()


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
