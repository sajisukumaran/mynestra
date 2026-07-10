from django.apps import AppConfig


class BankingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.banking"
    label = "banking"
    verbose_name = "Banking"

    # Launcher module metadata (DESIGN §9) is declared in a later commit alongside the dashboard.
