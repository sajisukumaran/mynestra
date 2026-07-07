"""Custom identity model (public schema).

Email is the login (no username). `theme` is a per-user light/dark preference (null → inherit the
household/system default). `default_tenant` is where the tenant chooser lands first. Invite-only
signup, memberships, and invitations arrive in P1.
"""

from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models

from .managers import UserManager


class User(AbstractBaseUser, PermissionsMixin):
    class Theme(models.TextChoices):
        LIGHT = "light", "Light"
        DARK = "dark", "Dark"

    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=150, blank=True)

    # Per-user theme preference. null is a deliberate third state — "inherit the household/system
    # default" — distinct from an explicitly chosen light/dark (DESIGN §4). Hence null=True.
    theme = models.CharField(max_length=5, choices=Theme.choices, null=True, blank=True)  # noqa: DJ001
    # Where the chooser lands first (nullable). SET_NULL so deleting a tenant never deletes a user.
    default_tenant = models.ForeignKey(
        "tenants.Tenant",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []  # email + password are prompted by createsuperuser automatically.

    class Meta:
        verbose_name = "user"
        verbose_name_plural = "users"

    def __str__(self) -> str:
        return self.email

    def get_full_name(self) -> str:
        return self.full_name or self.email

    def get_short_name(self) -> str:
        return self.full_name.split(" ")[0] if self.full_name else self.email
