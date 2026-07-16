"""Health module (Plan D) COA: turn the pre-seeded `5400 Health & Medical` leaf into a group header
(+ a role handle `health_medical`) so the Health module can post each provider invoice to the child
matching its encounter type, and add the per-type children `5410 Doctor/Medical` / `5420 Dental` /
`5430 Vision` / `5440 Pharmacy/Prescriptions` / `5450 Hospital`.

`seed_finance()` is idempotent (`update_or_create` on account code), so it flips the `5400` leaf to a
header and creates the new children in existing tenant schemas; brand-new tenants get them via the
`post_schema_sync` receiver. The leaf is flipped straight through the seed (bypassing `edit_account`'s
"no header with postings" guard), which is safe only because it is greenfield — so we assert no
journal lines reference it before reseeding (mirrors `0015_insurance_accounts` / `0016_realestate`).
"""

from django.db import migrations


def seed(apps, schema_editor):
    JournalLine = apps.get_model("finance", "JournalLine")
    if JournalLine.objects.filter(account__code="5400").exists():
        raise RuntimeError(
            "Cannot convert the 5400 Health & Medical leaf to a header: journal lines reference it."
        )
    from apps.finance.seed import seed_finance

    seed_finance()


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0016_realestate_accounts"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
