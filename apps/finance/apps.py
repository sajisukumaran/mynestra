from django.apps import AppConfig


class FinanceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.finance"
    label = "finance"
    verbose_name = "Finance"

    # NOTE: `launcher_module` + `launcher_counts()` (the launcher tile) are added in a later commit.
    # The core backbone is invisible; only Setup → Localization surfaces at first.
