"""Tenant-schema URLs — served under /t/<slug>/ by django-tenants.

MembershipMiddleware guards every route here (member-only). Feature apps (contacts, organizations,
setup, ...) mount their routes here in later phases.
"""

from django.urls import path

from apps.accounts import views as account_views
from apps.core.views import health

urlpatterns = [
    path("", account_views.tenant_home, name="tenant-home"),
    path("invite/", account_views.invite_create, name="invite-create"),
    path("health/", health, name="tenant-health"),
]
