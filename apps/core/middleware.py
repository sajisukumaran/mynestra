"""MembershipMiddleware — enforces per-tenant access (DESIGN §3 request lifecycle).

Public-schema requests pass through. For a tenant request (/t/<slug>/...): anonymous users are
redirected to login; authenticated non-members get a 403; members continue. Must sit AFTER
AuthenticationMiddleware (needs request.user) and after TenantSubfolderMiddleware (needs
request.tenant, which is first in MIDDLEWARE).
"""

from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponseForbidden
from django_tenants.utils import get_public_schema_name


class MembershipMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tenant = getattr(request, "tenant", None)
        if tenant is None or tenant.schema_name == get_public_schema_name():
            return self.get_response(request)

        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return redirect_to_login(request.get_full_path())

        # Membership lives in public; reachable here via the [tenant, public] search_path.
        from apps.tenants.models import Membership

        if not Membership.objects.filter(user=user, tenant=tenant).exists():
            return HttpResponseForbidden("You are not a member of this household.")

        return self.get_response(request)
