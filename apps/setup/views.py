"""Setup app views (tenant-scoped, Owner-only).

The Setup surface manages the household's catalogs and settings (DESIGN §8): categories,
relationship types, members & invitations, appearance, tenant profile, recently-deleted. Every
view is guarded by `owner_required`; feature templates compose cotton components only.
"""

from django.shortcuts import render

from apps.accounts.decorators import owner_required
from apps.relationships.models import PersonOrgRelationshipType, RelationshipType
from apps.setup.models import Category
from apps.tenants.models import Invitation, Membership


def setup_context(request, active, **extra):
    """Shared context for every Setup page: sidebar counts + which nav item is active."""
    nav_rel_types = RelationshipType.objects.count() + PersonOrgRelationshipType.objects.count()
    ctx = {
        "active": active,
        "nav_categories": Category.objects.count(),
        "nav_rel_types": nav_rel_types,
        "nav_members": Membership.objects.filter(tenant=request.tenant).count(),
        "nav_invites": Invitation.objects.filter(
            tenant=request.tenant, status=Invitation.Status.PENDING
        ).count(),
    }
    ctx.update(extra)
    return ctx


@owner_required
def overview(request):
    """Setup landing: quick links to each management area with live counts."""
    return render(request, "setup/overview.html", setup_context(request, "overview"))
