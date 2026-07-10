from django.apps import AppConfig


class InvestmentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.investments"
    label = "investments"
    verbose_name = "Investments"

    # Launcher module metadata (DESIGN §9). Read by apps.core.registry / the launcher.
    launcher_module = {
        "name": "Investments",
        "description": "Portfolio & holdings",
        "glyph": "trending-up",
        "tint": "investments",
        "url": "investments/",
        "order": 60,
    }

    def launcher_counts(self):
        from apps.investments.models import InvestmentAccount, Lot
        from apps.investments.services import total_portfolio_value

        accounts = InvestmentAccount.objects.all()
        holdings = (
            Lot.objects.filter(open=True)
            .values("account_id", "security_id")
            .distinct()
            .count()
        )
        return [
            {"n": accounts.count(), "label": "Accounts"},
            {"n": holdings, "label": "Holdings"},
            {"n": total_portfolio_value(), "label": "Value"},
        ]
