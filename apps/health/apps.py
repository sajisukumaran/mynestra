from django.apps import AppConfig


class HealthConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.health"
    label = "health"
    verbose_name = "Health"

    # Launcher module metadata (DESIGN §9). Read by apps.core.registry / the launcher.
    # (Attr is `launcher_module`, NOT `module`: AppConfig binds `.module` to the app's module.)
    launcher_module = {
        "name": "Health",
        "description": "Visits, bills & medications",
        "glyph": "activity",
        "tint": "health",
        "url": "health/",
        "order": 97,
    }

    def launcher_counts(self):
        from apps.health.services import launcher_counts

        return launcher_counts()
