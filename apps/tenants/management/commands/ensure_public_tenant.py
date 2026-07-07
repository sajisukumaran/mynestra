"""Idempotently ensure the `public` schema Tenant row exists.

django-tenants needs a Tenant row for the public schema so the subfolder middleware can serve
non-tenant paths (/health/, /login/, ...). This is tenancy *infrastructure* (P0), distinct from
household provisioning/seeding (P1). Safe to run on every boot.
"""

from django.core.management.base import BaseCommand
from django_tenants.utils import get_public_schema_name, get_tenant_model


class Command(BaseCommand):
    help = "Ensure the public-schema Tenant row exists (idempotent)."

    def handle(self, *args, **options):
        Tenant = get_tenant_model()
        public_schema = get_public_schema_name()
        _, created = Tenant.objects.get_or_create(
            schema_name=public_schema,
            defaults={"name": "Public"},
        )
        verb = "Created" if created else "Exists"
        self.stdout.write(self.style.SUCCESS(f"{verb}: public tenant ({public_schema})"))
