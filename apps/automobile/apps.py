from django.apps import AppConfig


class AutomobileConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.automobile"
    label = "automobile"
    verbose_name = "Vehicles"

    # Launcher module metadata (DESIGN §9). Read by apps.core.registry / the launcher.
    # (Attr is `launcher_module`, NOT `module`: AppConfig binds `.module` to the app's module.)
    launcher_module = {
        "name": "Vehicles",
        "description": "Cars, service & insurance",
        "glyph": "car",
        "tint": "automobile",
        "url": "automobile/",
        "order": 90,
    }

    def launcher_counts(self):
        from apps.automobile.services import launcher_counts

        return launcher_counts()
