from django.apps import AppConfig


class FinanceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.finance"
    label = "finance"
    verbose_name = "Finance"

    # Launcher module metadata (DESIGN §9). Read by apps.core.registry / the launcher.
    # (Attr is `launcher_module`, NOT `module`: AppConfig binds `.module` to the app's module.)
    launcher_module = {
        "name": "Finance",
        "description": "Accounts, ledger & balances",
        "glyph": "coins",
        "tint": "finance",
        "url": "finance/",
        "order": 30,
    }

    def launcher_counts(self):
        from apps.finance.models import Account, Currency, JournalEntry

        return [
            {"n": Account.objects.filter(is_postable=True).count(), "label": "Accounts"},
            {"n": JournalEntry.objects.filter(status=JournalEntry.Status.POSTED).count(),
             "label": "Journal entries"},
            {"n": Currency.objects.filter(is_active=True).count(), "label": "Currencies"},
        ]
