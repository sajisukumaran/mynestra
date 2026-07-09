from django.apps import AppConfig


class ContactsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.contacts"
    label = "contacts"
    verbose_name = "Contacts"

    # Launcher module metadata (DESIGN §9). Read by apps.core.registry / the launcher.
    module = {
        "name": "Contacts",
        "description": "People, families & relationships",
        "glyph": "users",
        "tint": "contacts",
        "url": "contacts/",
        "order": 10,
    }

    def launcher_counts(self):
        from apps.contacts.models import Person
        from apps.contacts.services import count_birthdays
        from apps.families.models import Family

        return [
            {"n": Person.objects.count(), "label": "People"},
            {"n": Family.objects.count(), "label": "Families"},
            {"n": count_birthdays(30), "label": "Birthdays"},
        ]
