"""Loans & Liabilities module COA: turn the pre-seeded loan-type leaves (`2210 Mortgage`,
`2220 Auto Loan`, `2230 Personal Loan`) into per-type group headers and give `2200 Loans` a role
handle, so the Loans module can nest one postable sub-account per loan under the header matching its
`loan_type`. Adds the remaining type headers (`2240 Student Loan`, `2250 HELOC`,
`2260 Line of Credit`), `2900 Other Liabilities`, `2950 Contingent Liabilities` (off-balance-sheet,
excluded from net worth), and escrow expense accounts `5140 Property Tax` / `5150 Home Insurance`.

`seed_finance()` is idempotent (`update_or_create` on account code), so it flips the leaves to
headers and creates the new accounts in existing tenant schemas; brand-new tenants get them via the
`post_schema_sync` receiver. The leaves are flipped straight through the seed (bypassing
`edit_account`'s "no header with postings" guard), which is safe only because they are greenfield —
so we assert no journal lines reference them before reseeding.
"""

from django.db import migrations


def seed(apps, schema_editor):
    JournalLine = apps.get_model("finance", "JournalLine")
    if JournalLine.objects.filter(account__code__in=["2210", "2220", "2230"]).exists():
        raise RuntimeError(
            "Cannot convert loan-type leaves 2210/2220/2230 to headers: journal lines exist."
        )
    from apps.finance.seed import seed_finance

    seed_finance()


class Migration(migrations.Migration):
    dependencies = [
        ("finance", "0010_payables_accounts"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
