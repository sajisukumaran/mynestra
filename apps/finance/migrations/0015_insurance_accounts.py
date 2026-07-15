"""Insurance module COA: turn the pre-seeded `5500 Insurance` leaf into a group header (so the
Insurance module can post each policy's premiums to the child matching its policy type) and add the
per-type children `5510 Health` / `5520 Life` / `5530 Umbrella-Liability` / `5540 Renters` /
`5590 Other Insurance`. Auto insurance keeps its own `5340 Vehicle Insurance`; home / mortgage-escrow
insurance keeps `5150 Home Insurance`.

`seed_finance()` is idempotent (`update_or_create` on account code), so it flips the `5500` leaf to a
header and creates the new children in existing tenant schemas; brand-new tenants get them via the
`post_schema_sync` receiver. The leaf is flipped straight through the seed (bypassing `edit_account`'s
"no header with postings" guard), which is safe only because it is greenfield — so we assert no journal
lines reference it before reseeding (mirrors `0013_vehicle_accounts`).
"""

from django.db import migrations


def seed(apps, schema_editor):
    JournalLine = apps.get_model("finance", "JournalLine")
    if JournalLine.objects.filter(account__code="5500").exists():
        raise RuntimeError(
            "Cannot convert the 5500 Insurance leaf to a header: journal lines reference it."
        )
    from apps.finance.seed import seed_finance

    seed_finance()


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0014_property_tax_account"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
