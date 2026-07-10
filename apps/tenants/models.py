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

# Curated timezone list for Setup → Localization (validated against on save; not free text).
CURATED_TIMEZONES = [
    "UTC",
    "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
    "America/Toronto", "America/Sao_Paulo",
    "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Madrid", "Europe/Moscow",
    "Africa/Johannesburg",
    "Asia/Dubai", "Asia/Kolkata", "Asia/Singapore", "Asia/Hong_Kong", "Asia/Shanghai",
    "Asia/Tokyo",
    "Australia/Sydney", "Pacific/Auckland",
]


class Tenant(TenantMixin):
    class Palette(models.TextChoices):
        TEAL = "teal", "Teal"
        INDIGO = "indigo", "Indigo"
        BLUE = "blue", "Blue"
        VIOLET = "violet", "Violet"
        GRAPHITE = "graphite", "Graphite"

    class DateFormat(models.TextChoices):
        ISO = "iso", "2026-07-09"
        DMY = "dmy", "09-07-2026"
        MDY = "mdy", "07/09/2026"
        LONG = "long", "09 Jul 2026"

    class NumberFormat(models.TextChoices):
        PLAIN = "plain", "1234.56"
        THOUSANDS = "thousands", "1,234.56"
        INDIAN = "indian", "1,23,456.78"

    class AccountingMode(models.TextChoices):
        # Standard: the GL is invisible; the software picks every account. Expert: the household
        # controls the Chart of Accounts + per-account posting maps. Switch to Expert freely; back
        # to Standard only while `accounting_locked` is False (see the Setup Mode screen).
        STANDARD = "standard", "Standard"
        EXPERT = "expert", "Expert"

    # `schema_name` (max 63, unique) comes from TenantMixin and doubles as the slug.
    name = models.CharField(max_length=100)
    # Household accent, chosen by an Owner in Setup → Appearance (P3). Default Teal (DESIGN §7.2).
    palette = models.CharField(max_length=16, choices=Palette.choices, default=Palette.TEAL)
    logo = models.ImageField(upload_to="tenant_logos/", null=True, blank=True)
    created_on = models.DateField(auto_now_add=True)

    # Localization (Setup → Localization, Owner-set). `currency` is the finance base/functional
    # currency (a code from the tenant-schema Currency catalog); the rest drive money/date display.
    currency = models.CharField(max_length=3, default="USD")
    timezone = models.CharField(max_length=64, default="UTC")
    date_format = models.CharField(
        max_length=8, choices=DateFormat.choices, default=DateFormat.ISO
    )
    number_format = models.CharField(
        max_length=12, choices=NumberFormat.choices, default=NumberFormat.THOUSANDS
    )

    # Accounting mode (Setup → Mode, Owner-set). Default Standard: the double-entry GL is entirely
    # behind the scenes. `accounting_locked` turns True the first time an Expert user makes a
    # Standard-critical Chart-of-Accounts edit (delete/deactivate/re-code/reparent/un-header a
    # seeded account); once locked, the household can no longer switch back to Standard.
    accounting_mode = models.CharField(
        max_length=8, choices=AccountingMode.choices, default=AccountingMode.STANDARD
    )
    accounting_locked = models.BooleanField(default=False)

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
