"""Public-schema URLs (served for every path NOT under /t/<slug>/)."""

from django.contrib import admin
from django.urls import path

from apps.core.views import health

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", health, name="health"),
    # Landing / tenant chooser arrives in P1; for now the root serves the health page.
    path("", health, name="home"),
]
