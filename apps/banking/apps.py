from django.apps import AppConfig


class BankingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.banking"
    label = "banking"
    verbose_name = "Banking"

    # Launcher module metadata (DESIGN §9). Read by apps.core.registry / the launcher.
    launcher_module = {
        "name": "Banking",
        "description": "Accounts & transactions",
        "glyph": "landmark",
        "tint": "banking",
        "url": "banking/",
        "order": 40,
    }

    def launcher_counts(self):
        from apps.banking.models import BankAccount, BankTransaction

        accounts = BankAccount.objects.all()
        return [
            {"n": accounts.count(), "label": "Accounts"},
            {"n": BankTransaction.objects.count(), "label": "Transactions"},
            {"n": len({a.bank_id for a in accounts}), "label": "Banks"},
        ]
