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
    path("contacts/", include("apps.contacts.urls")),
    path("organizations/", include("apps.organizations.urls")),
    path("finance/", include("apps.finance.urls")),
    path("banking/", include("apps.banking.urls")),
    path("setup/", include("apps.setup.urls")),
    # Legacy P1 invite route; invitations now live in Setup → Members (P3).
    path(
        "invite/",
        RedirectView.as_view(url="../setup/members/", permanent=False),
        name="invite-create",
    ),
    path("health/", health, name="tenant-health"),
]

# Error handlers must be defined on the tenant urlconf: django-tenants' urlconf wrapper raises
# ImportError (instead of falling back to Django's defaults) when these are absent, so an Http404
# raised in a tenant view would 500 under DEBUG=False. The default views render our on-brand
# templates/{400,403,404,500}.html (P7); MembershipMiddleware raises PermissionDenied → handler403.
handler400 = "django.views.defaults.bad_request"
handler403 = "django.views.defaults.permission_denied"
handler404 = "django.views.defaults.page_not_found"
handler500 = "django.views.defaults.server_error"
