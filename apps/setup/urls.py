"""Setup URLs — mounted under /t/<slug>/setup/ (see config/urls_tenants.py). Owner-only.

Reversing is not subfolder-aware under django-tenants, so templates build links from
`ui_tenant_url` (e.g. "/t/<slug>/setup/..."); these names exist for clarity/tests.
"""

from django.urls import path

from apps.setup import views

app_name = "setup"

urlpatterns = [
    path("", views.overview, name="overview"),
]
