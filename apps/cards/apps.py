from django.apps import AppConfig


class CardsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.cards"
    label = "cards"
    verbose_name = "Cards"

    # Launcher module metadata (DESIGN §9). Read by apps.core.registry / the launcher.
    # (Attr is `launcher_module`, NOT `module`: AppConfig binds `.module` to the app's module.)
    launcher_module = {
        "name": "Cards",
        "description": "Credit & debit cards",
        "glyph": "credit-card",
        "tint": "cards",
        "url": "cards/",
        "order": 50,
    }

    def launcher_counts(self):
        from apps.cards.models import CreditCard, DebitCard
        from apps.cards.services import total_owed

        return [
            {"n": CreditCard.objects.count(), "label": "Credit cards"},
            {"n": DebitCard.objects.count(), "label": "Debit cards"},
            {"n": total_owed(), "label": "Owed"},
        ]
