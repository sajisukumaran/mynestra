"""Add the generic `5810 Property Tax` expense account (a sibling of `5800 Taxes`) — the home for
the Automobile module's personal-property tax, and the future Real Estate module's real-estate
property tax. `seed_finance()` is idempotent (`update_or_create` on account code), so this is a pure
add: it creates 5810 in existing tenant schemas, and brand-new tenants get it via the
`post_schema_sync` receiver. No greenfield guard is needed (nothing is flipped or removed), unlike
`0013_vehicle_accounts`.
"""

from django.db import migrations


def seed(apps, schema_editor):
    from apps.finance.seed import seed_finance

    seed_finance()


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0013_vehicle_accounts"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
