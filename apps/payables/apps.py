from django.apps import AppConfig


class PayablesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.payables"
    label = "payables"
    verbose_name = "Payables"

    # Launcher module metadata (DESIGN §9). Read by apps.core.registry / the launcher.
    launcher_module = {
        "name": "Payables",
        "description": "Vendor bills & payments",
        "glyph": "file-text",
        "tint": "payables",
        "url": "payables/",
        "order": 70,
    }

    def launcher_counts(self):
        from apps.payables.models import Bill, Payment, VendorProfile

        open_bills = Bill.objects.filter(
            status__in=[Bill.Status.OPEN, Bill.Status.PARTIALLY_PAID]
        ).count()
        return [
            {"n": open_bills, "label": "Open bills"},
            {"n": VendorProfile.objects.count(), "label": "Vendors"},
            {"n": Payment.objects.count(), "label": "Payments"},
        ]
