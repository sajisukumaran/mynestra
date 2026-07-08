"""Tenancy + membership models (public schema).

`Tenant`/`Domain` are the django-tenants substrate (subfolder routing; slug == schema_name).
`Membership` and `Invitation` live here too (DESIGN §3: the tenants app owns Tenant, Domain,
Membership, Invitation) and are public so the tenant chooser can query them across households.
"""

import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone
from django_tenants.models import DomainMixin, TenantMixin


class Tenant(TenantMixin):
    class Palette(models.TextChoices):
        TEAL = "teal", "Teal"
        INDIGO = "indigo", "Indigo"
        BLUE = "blue", "Blue"
        VIOLET = "violet", "Violet"
        GRAPHITE = "graphite", "Graphite"

    # `schema_name` (max 63, unique) comes from TenantMixin and doubles as the slug.
    name = models.CharField(max_length=100)
    # Household accent, chosen by an Owner in Setup → Appearance (P3). Default Teal (DESIGN §7.2).
    palette = models.CharField(max_length=16, choices=Palette.choices, default=Palette.TEAL)
    logo = models.ImageField(upload_to="tenant_logos/", null=True, blank=True)
    created_on = models.DateField(auto_now_add=True)

    auto_create_schema = True
    auto_drop_schema = False

    @property
    def slug(self) -> str:
        """The /t/<slug>/ segment — identical to the schema name (DESIGN: 'slug = schema')."""
        return self.schema_name

    def __str__(self) -> str:
        return self.name


class Domain(DomainMixin):
    # DomainMixin provides `domain` (holds the slug for routing), `tenant` FK, and `is_primary`.
    pass


# Roles are stored as a short string so adding a role never needs a migration (DESIGN §4):
# the field below deliberately has no `choices=`. These constants are for use in code.
class Role:
    OWNER = "OWNER"
    MEMBER = "MEMBER"


class Membership(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships"
    )
    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="memberships"
    )
    role = models.CharField(max_length=20, default=Role.MEMBER)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "tenant"], name="uniq_membership_user_tenant"),
        ]

    def __str__(self) -> str:
        return f"{self.user} @ {self.tenant} ({self.role})"

    @property
    def is_owner(self) -> bool:
        return self.role == Role.OWNER


def _default_invitation_token() -> str:
    return secrets.token_urlsafe(32)


def _default_invitation_expiry():
    return timezone.now() + timedelta(days=7)


class Invitation(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        ACCEPTED = "ACCEPTED", "Accepted"
        REVOKED = "REVOKED", "Revoked"
        EXPIRED = "EXPIRED", "Expired"

    email = models.EmailField()
    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.CASCADE, related_name="invitations"
    )
    role = models.CharField(max_length=20, default=Role.MEMBER)
    token = models.CharField(
        max_length=64, unique=True, default=_default_invitation_token, editable=False
    )
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    expires_at = models.DateTimeField(default=_default_invitation_expiry)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"Invite {self.email} → {self.tenant} ({self.status})"

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @property
    def is_actionable(self) -> bool:
        """Pending and not past its expiry — i.e. can still be accepted."""
        return self.status == self.Status.PENDING and not self.is_expired

    def get_accept_path(self) -> str:
        """Un-prefixed public accept path (mounted in PUBLIC_SCHEMA_URLCONF)."""
        return f"/invite/{self.token}/"
