from django.apps import AppConfig


class OrganizationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.organizations"
    label = "organizations"
    verbose_name = "Organizations"

    # Launcher module metadata (DESIGN §9). Read by apps.core.registry / the launcher.
    module = {
        "name": "Organizations",
        "description": "Companies, branches & key people",
        "glyph": "building-2",
        "tint": "orgs",
        "url": "organizations/",
        "order": 20,
    }

    def launcher_counts(self):
        from apps.organizations.models import Branch, Organization
        from apps.relationships.models import PersonOrgRelationship

        return [
            {"n": Organization.objects.count(), "label": "Organizations"},
            {"n": Branch.objects.count(), "label": "Branches"},
            {"n": PersonOrgRelationship.objects.values("person").distinct().count(),
             "label": "Key people"},
        ]
