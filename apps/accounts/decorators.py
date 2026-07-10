"""Access decorators for tenant-scoped views.

`owner_required` gates the Setup surface (DESIGN §4: "Owner-only gates: Setup, invitations,
appearance-for-household, danger zone, hard delete"). MembershipMiddleware has already proven the
user is a member of `request.tenant` by the time a tenant view runs; this narrows that to OWNER.
"""

from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.http import Http404, HttpResponseForbidden

from apps.tenants.models import Membership, Role


def expert_required(view):
    """Allow only when `request.tenant` is in Expert accounting mode; else 404 — the Finance/GL
    surface is hidden entirely in Standard mode (the software handles accounting behind the scenes).
    MembershipMiddleware has already proven membership. Stack above `owner_required` when a view is
    both Expert-only and Owner-only (mode check first → 404 hides it before the 403)."""

    @wraps(view)
    def _wrapped(request, *args, **kwargs):
        mode = getattr(getattr(request, "tenant", None), "accounting_mode", "standard")
        if mode != "expert":
            raise Http404()
        return view(request, *args, **kwargs)

    return _wrapped


def owner_required(view):
    """Allow only Owners of `request.tenant`. Anonymous → login; non-owner member → 403."""

    @wraps(view)
    def _wrapped(request, *args, **kwargs):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return redirect_to_login(request.get_full_path())

        is_owner = Membership.objects.filter(
            user=user, tenant=request.tenant, role=Role.OWNER
        ).exists()
        if not is_owner:
            return HttpResponseForbidden("Only household owners can access Setup.")

        return view(request, *args, **kwargs)

    return _wrapped
