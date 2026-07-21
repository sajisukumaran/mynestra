from django.apps import AppConfig


class LoansConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.loans"
    label = "loans"
    verbose_name = "Loans & Liabilities"

    # Launcher module metadata (DESIGN §9). Read by apps.core.registry / the launcher.
    # (Attr is `launcher_module`, NOT `module`: AppConfig binds `.module` to the app's module.)
    launcher_module = {
        "name": "Loans",
        "description": "Loans & liabilities",
        "glyph": "banknote",
        "tint": "loans",
        "url": "loans/",
        "order": 80,
    }

    def launcher_counts(self):
        from apps.loans.services import dashboard_stats

        stats = dashboard_stats()
        return [
            {"n": stats["loans_count"], "label": "Loans"},
            {"n": stats["total_owed"], "label": "Owed", "money": True},
        ]
