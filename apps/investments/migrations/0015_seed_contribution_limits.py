"""Seed the editable ContributionLimit table with recent IRS figures (formerly the hardcoded
CONTRIBUTION_LIMITS dict). Runs per tenant schema. Idempotent via update_or_create; households
edit these or add more years in Setup (Setup ▸ Contribution limits) — no code change needed.
Sources: IRA Pub 590-A; HSA Rev. Proc. / Pub 969. VERIFY future years against current IRS figures."""

from decimal import Decimal

from django.db import migrations

SEED = {
    2023: {"ira": "6500", "ira_catchup": "1000",
           "hsa_self": "3850", "hsa_family": "7750", "hsa_catchup": "1000"},
    2024: {"ira": "7000", "ira_catchup": "1000",
           "hsa_self": "4150", "hsa_family": "8300", "hsa_catchup": "1000"},
    2025: {"ira": "7000", "ira_catchup": "1000",
           "hsa_self": "4300", "hsa_family": "8550", "hsa_catchup": "1000"},
    2026: {"ira": "7500", "ira_catchup": "1100",
           "hsa_self": "4400", "hsa_family": "8750", "hsa_catchup": "1000"},
}


def seed(apps, schema_editor):
    ContributionLimit = apps.get_model("investments", "ContributionLimit")
    for year, vals in SEED.items():
        ContributionLimit.objects.update_or_create(
            tax_year=year, defaults={k: Decimal(v) for k, v in vals.items()}
        )


class Migration(migrations.Migration):

    dependencies = [
        ("investments", "0014_contributionlimit"),
    ]

    # Reverse is a no-op: never delete a household's limit rows on rollback.
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
