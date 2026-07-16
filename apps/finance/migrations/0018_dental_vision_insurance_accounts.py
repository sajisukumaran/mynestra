"""Health module (Plan D, P2) COA: add the dental / vision insurance premium homes
`5550 Dental Insurance` (`dental_insurance`) and `5560 Vision Insurance` (`vision_insurance`) under
the existing `5500 Insurance` header. A pure add — no existing account changes shape — so this is
NOT greenfield-guarded (unlike the header promotions).

`seed_finance()` is idempotent (`update_or_create` on account code), so it creates the two children
in existing tenant schemas under `migrate_schemas`; brand-new tenants get them via the
`post_schema_sync` receiver.
"""

from django.db import migrations


def seed(apps, schema_editor):
    from apps.finance.seed import seed_finance

    seed_finance()


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0017_health_accounts"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
