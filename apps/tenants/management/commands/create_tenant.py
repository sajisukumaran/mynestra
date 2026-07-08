"""Create a household (tenant): schema + §6 seed (via signal) + Domain + founding OWNER membership.

The owner user must already exist (create it first, or use `bootstrap` for a cold start).
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.tenants.models import Domain, Membership, Role, Tenant


class Command(BaseCommand):
    help = "Create a tenant with a founding OWNER membership."

    def add_arguments(self, parser):
        parser.add_argument("--name", required=True, help="Household display name")
        parser.add_argument("--slug", required=True, help="Schema/slug (lowercase; = /t/<slug>/)")
        parser.add_argument("--owner-email", required=True, help="Existing user's email")
        parser.add_argument("--role", default=Role.OWNER)

    def handle(self, *args, **options):
        User = get_user_model()
        slug = options["slug"]
        email = options["owner_email"].lower()

        try:
            owner = User.objects.get(email=email)
        except User.DoesNotExist as exc:
            raise CommandError(
                f"No user with email {email!r}. Create the user first (or use `bootstrap`)."
            ) from exc

        if Tenant.objects.filter(schema_name=slug).exists():
            raise CommandError(f"Tenant {slug!r} already exists.")

        tenant = Tenant(schema_name=slug, name=options["name"])
        tenant.save()  # CREATE SCHEMA + migrate TENANT_APPS + seed §6 via post_schema_sync

        Domain.objects.get_or_create(domain=slug, tenant=tenant, defaults={"is_primary": True})
        Membership.objects.get_or_create(
            user=owner, tenant=tenant, defaults={"role": options["role"]}
        )

        self.stdout.write(self.style.SUCCESS(
            f"Created tenant {slug!r} ({tenant.name}) with {options['role']} {owner.email}. "
            f"Visit /t/{slug}/"
        ))
