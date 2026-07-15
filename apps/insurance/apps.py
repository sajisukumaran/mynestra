from django.apps import AppConfig


class InsuranceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.insurance"
    label = "insurance"
    verbose_name = "Insurance"

    # Launcher module metadata (DESIGN §9). Read by apps.core.registry / the launcher.
    # (Attr is `launcher_module`, NOT `module`: AppConfig binds `.module` to the app's module.)
    launcher_module = {
        "name": "Insurance",
        "description": "Policies, premiums & claims",
        "glyph": "shield-check",
        "tint": "insurance",
        "url": "insurance/",
        "order": 95,
    }

    def launcher_counts(self):
        from apps.insurance.services import launcher_counts

        return launcher_counts()
