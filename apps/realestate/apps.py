from django.apps import AppConfig


class RealEstateConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.realestate"
    label = "realestate"
    verbose_name = "Real Estate"

    # Launcher module metadata (DESIGN §9). Read by apps.core.registry / the launcher.
    # (Attr is `launcher_module`, NOT `module`: AppConfig binds `.module` to the app's module.)
    launcher_module = {
        "name": "Real Estate",
        "description": "Homes & property, costs & taxes",
        "glyph": "house",
        "tint": "realestate",
        "url": "realestate/",
        "order": 96,
    }

    def launcher_counts(self):
        from apps.realestate.services import launcher_counts

        return launcher_counts()
