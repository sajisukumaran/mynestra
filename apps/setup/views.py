"""Setup app views (tenant-scoped, Owner-only).

The Setup surface manages the household's catalogs and settings (DESIGN §8): categories,
relationship types, members & invitations, appearance, tenant profile, recently-deleted. Every
view is guarded by `owner_required`; feature templates compose cotton components only.
"""

from django.http import Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render

from apps.accounts.decorators import owner_required
from apps.relationships.models import PersonOrgRelationshipType, RelationshipType
from apps.setup.forms import CategoryForm
from apps.setup.models import Category
from apps.tenants.models import Invitation, Membership


def setup_url(request, path=""):
    """Absolute Setup URL for redirects (reversing is not subfolder-aware under django-tenants)."""
    return f"/t/{request.tenant.schema_name}/setup/{path}"


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


# --- Categories -----------------------------------------------------------------------------
# System rows (is_system=True, seeded §6) are locked: they cannot be edited or deleted. The lock
# is enforced here (server-side, so a crafted POST is rejected) and reflected in the UI.


@owner_required
def categories(request):
    """List Person & Org categories; system rows show a lock, custom rows can be edited/deleted."""
    groups = [
        {
            "label": "Person categories",
            "kind": Category.Kind.PERSON,
            "items": Category.objects.filter(kind=Category.Kind.PERSON),
        },
        {
            "label": "Organization categories",
            "kind": Category.Kind.ORG,
            "items": Category.objects.filter(kind=Category.Kind.ORG),
        },
    ]
    ctx = setup_context(request, "categories", groups=groups)
    return render(request, "setup/categories.html", ctx)


@owner_required
def category_create(request, kind):
    kind = kind.upper()
    if kind not in Category.Kind.values:
        raise Http404()

    form = CategoryForm(request.POST or None, instance=Category(kind=kind))
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect(setup_url(request, "categories/"))

    kind_label = "Person" if kind == Category.Kind.PERSON else "Organization"
    ctx = setup_context(
        request, "categories",
        form=form, kind=kind, kind_label=kind_label, mode="create",
    )
    return render(request, "setup/category_form.html", ctx)


@owner_required
def category_edit(request, pk):
    category = get_object_or_404(Category, pk=pk)
    if category.is_system:
        return HttpResponseForbidden("System categories are locked and cannot be edited.")

    form = CategoryForm(request.POST or None, instance=category)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect(setup_url(request, "categories/"))

    kind_label = "Person" if category.kind == Category.Kind.PERSON else "Organization"
    ctx = setup_context(
        request, "categories",
        form=form, category=category, kind=category.kind, kind_label=kind_label, mode="edit",
    )
    return render(request, "setup/category_form.html", ctx)


@owner_required
def category_delete(request, pk):
    category = get_object_or_404(Category, pk=pk)
    if category.is_system:
        return HttpResponseForbidden("System categories are locked and cannot be deleted.")
    if request.method == "POST":
        category.delete()  # hard delete: P3 catalogs have no dependents yet (soft-delete lands P4)
    return redirect(setup_url(request, "categories/"))
