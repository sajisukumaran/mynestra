"""Tenant-schema URLs — served under /t/<slug>/ by django-tenants.

MembershipMiddleware guards every route here (member-only). Feature apps (contacts, organizations,
setup, ...) mount their routes here in later phases.
"""

from django.urls import include, path
from django.views.generic import RedirectView

from apps.accounts import views as account_views
from apps.core.views import health

urlpatterns = [
    path("", account_views.tenant_home, name="tenant-home"),
    path("setup/", include("apps.setup.urls")),
    # Legacy P1 invite route; invitations now live in Setup → Members (P3).
    path("invite/", RedirectView.as_view(url="../setup/members/", permanent=False), name="invite-create"),
    path("health/", health, name="tenant-health"),
]
