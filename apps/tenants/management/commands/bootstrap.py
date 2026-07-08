"""Cold-start bootstrap: create the first user + first household + OWNER membership.

Yields a working login for local dev / demo. Idempotent-friendly (safe to re-run).
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from apps.tenants.models import Domain, Membership, Role, Tenant


class Command(BaseCommand):
    help = "Create the first user + first tenant + OWNER membership (cold start / demo)."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--password", required=True)
        parser.add_argument("--full-name", default="")
        parser.add_argument("--name", default="Demo Household", help="Tenant display name")
        parser.add_argument("--slug", default="demo", help="Tenant schema/slug")

    def handle(self, *args, **options):
        User = get_user_model()
        email = options["email"].lower()
        slug = options["slug"]

        user, created = User.objects.get_or_create(
            email=email, defaults={"full_name": options["full_name"], "is_staff": True}
        )
        if created:
            user.set_password(options["password"])
            user.save()

        tenant = Tenant.objects.filter(schema_name=slug).first()
        if tenant is None:
            tenant = Tenant(schema_name=slug, name=options["name"])
            tenant.save()  # schema + migrations + §6 seed
            Domain.objects.get_or_create(domain=slug, tenant=tenant, defaults={"is_primary": True})

        Membership.objects.get_or_create(user=user, tenant=tenant, defaults={"role": Role.OWNER})

        if user.default_tenant_id is None:
            user.default_tenant = tenant
            user.save(update_fields=["default_tenant"])

        self.stdout.write(self.style.SUCCESS("Bootstrap complete."))
        self.stdout.write(f"  Login:   {user.email}")
        self.stdout.write(f"  Tenant:  /t/{tenant.slug}/  ({tenant.name})")
