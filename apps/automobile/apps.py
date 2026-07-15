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
        # Self-contained (no services import): the service-layer read models arrive in a later
        # commit. Two live counts — Vehicles and Fleet value at cost.
        from apps.automobile.models import Vehicle
        from apps.finance.models import ZERO

        vehicles = list(Vehicle.objects.filter(is_active=True))
        fleet = sum((v.cost for v in vehicles), ZERO)
        return [
            {"n": len(vehicles), "label": "Vehicles"},
            {"n": fleet, "label": "Fleet value"},
        ]
