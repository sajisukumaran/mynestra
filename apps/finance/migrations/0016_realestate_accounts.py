"""Real Estate module COA: turn the pre-seeded `1410 Real Estate` leaf into a group header (+ a
role handle `real_estate`), so the Real Estate module can nest one postable sub-account per owned
property under it (held at cost). Adds `5160 HOA & Condo Fees` (a distinct recurring housing cost).

`seed_finance()` is idempotent (`update_or_create` on account code), so it flips the `1410` leaf to
a header and creates `5160` in existing tenant schemas; brand-new tenants get them via the
`post_schema_sync` receiver. The leaf is flipped straight through the seed (bypassing
`edit_account`'s "no header with postings" guard), which is safe only because it is greenfield — so
we assert no journal lines reference it before reseeding (mirrors `0013_vehicle_accounts`).
"""

from django.db import migrations


def seed(apps, schema_editor):
    JournalLine = apps.get_model("finance", "JournalLine")
    if JournalLine.objects.filter(account__code="1410").exists():
        raise RuntimeError(
            "Cannot convert the 1410 Real Estate leaf to a header: journal lines reference it."
        )
    from apps.finance.seed import seed_finance

    seed_finance()


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0015_insurance_accounts"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
