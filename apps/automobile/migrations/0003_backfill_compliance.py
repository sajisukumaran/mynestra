"""Backfill the new compliance records from the legacy scalar Vehicle fields, so history is not
lost when the records become the single source of truth:

- Each vehicle with a plate / jurisdiction / registration expiry and NO registration term yet gets
  one INITIAL `VehicleRegistration` snapshotting the current plate/title/state + expiry.
- Each vehicle with an `inspection_due` and NO inspection yet gets one PASS safety
  `VehicleInspection` whose `expires_on` is that due date.

Idempotent (guarded on `.exists()`), so re-runs are safe. No property-tax backfill (no legacy
scalar). Runs per tenant schema (Automobile is a tenant app). `noop` on reverse.
"""

import datetime

from django.db import migrations


def backfill(apps, schema_editor):
    Vehicle = apps.get_model("automobile", "Vehicle")
    VehicleRegistration = apps.get_model("automobile", "VehicleRegistration")
    VehicleInspection = apps.get_model("automobile", "VehicleInspection")
    today = datetime.date.today()

    for v in Vehicle.objects.all():
        # A sensible effective date: the acquisition date if known, else today.
        if v.acquired_year:
            effective_from = datetime.date(
                v.acquired_year, v.acquired_month or 1, v.acquired_day or 1
            )
        else:
            effective_from = today

        if (v.license_plate or v.plate_jurisdiction or v.registration_expiry) \
                and not VehicleRegistration.objects.filter(vehicle=v).exists():
            VehicleRegistration.objects.create(
                vehicle=v,
                jurisdiction=v.plate_jurisdiction or "",
                plate_number=v.license_plate or "",
                plate_type="standard",
                title_number=v.title_number or "",
                title_status="clean",
                effective_from=effective_from,
                expires_on=v.registration_expiry,
                reason="initial",
            )

        if v.inspection_due and not VehicleInspection.objects.filter(vehicle=v).exists():
            VehicleInspection.objects.create(
                vehicle=v,
                kind="safety",
                performed_on=min(effective_from, v.inspection_due),
                result="pass",
                expires_on=v.inspection_due,
            )


class Migration(migrations.Migration):
    dependencies = [
        ("automobile", "0002_compliance"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
