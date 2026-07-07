"""Tenant-schema URLs — served under /t/<slug>/ by django-tenants.

Feature apps (contacts, organizations, setup, ...) mount their routes here in later phases.
"""

from django.urls import path

from apps.core.views import health

urlpatterns = [
    path("health/", health, name="tenant-health"),
]
