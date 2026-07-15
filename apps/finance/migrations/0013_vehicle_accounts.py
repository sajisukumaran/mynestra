"""Automobile module COA: turn the pre-seeded `1420 Vehicles` leaf into a group header (+ a role
handle `vehicles`), so the Automobile module can nest one postable sub-account per owned vehicle
under it (held at cost). Adds `1320 Refundable Deposits` (lease security deposits held as an asset),
`4930 Gain/Loss on Asset Sale` (a single disposal gain/loss account, REVENUE-typed so it can run
negative), and the running-cost expense homes `5340 Vehicle Insurance` / `5350 Vehicle Registration`
/ `5360 Vehicle Lease`.

`seed_finance()` is idempotent (`update_or_create` on account code), so it flips the `1420` leaf to
a header and creates the new accounts in existing tenant schemas; brand-new tenants get them via the
`post_schema_sync` receiver. The leaf is flipped straight through the seed (bypassing
`edit_account`'s "no header with postings" guard), which is safe only because it is greenfield — so
we assert no journal lines reference it before reseeding (mirrors `0011_loans_accounts`).
"""

from django.db import migrations


def seed(apps, schema_editor):
    JournalLine = apps.get_model("finance", "JournalLine")
    if JournalLine.objects.filter(account__code="1420").exists():
        raise RuntimeError(
            "Cannot convert the 1420 Vehicles leaf to a header: journal lines reference it."
        )
    from apps.finance.seed import seed_finance

    seed_finance()


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0012_journalline_jline_account_entry"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
