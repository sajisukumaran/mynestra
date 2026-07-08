"""Setup app views (tenant-scoped, Owner-only).

The Setup surface manages the household's catalogs and settings (DESIGN §8): categories,
relationship types, members & invitations, appearance, tenant profile, recently-deleted. Every
view is guarded by `owner_required`; feature templates compose cotton components only.
"""

from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.accounts.decorators import owner_required
from apps.relationships.models import PersonOrgRelationshipType, RelationshipType
from apps.setup.forms import (
    CategoryForm,
    InviteForm,
    PersonOrgRelationshipTypeForm,
    RelationshipTypeForm,
)
from apps.setup.models import Category
from apps.tenants.models import Invitation, Membership, Role, Tenant

# Relationship-type kinds: URL segment -> (model, form, is_p2p). P2P carries gender-aware labels;
# P2O carries a single label (DESIGN §5).
REL_KINDS = {
    "p2p": (RelationshipType, RelationshipTypeForm, True),
    "p2o": (PersonOrgRelationshipType, PersonOrgRelationshipTypeForm, False),
}


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


# --- Relationship types (P2P + P2O) ---------------------------------------------------------
# System rows are locked identically to categories: edit/delete refused server-side + hidden in UI.


@owner_required
def relationship_types(request):
    """List P2P types (gender-aware labels) and P2O types; system rows locked."""
    ctx = setup_context(
        request, "rel-types",
        p2p_types=RelationshipType.objects.all(),
        p2o_types=PersonOrgRelationshipType.objects.all(),
    )
    return render(request, "setup/relationship_types.html", ctx)


@owner_required
def rel_type_create(request, kind):
    if kind not in REL_KINDS:
        raise Http404()
    model, form_class, is_p2p = REL_KINDS[kind]

    form = form_class(request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect(setup_url(request, "relationship-types/"))

    ctx = setup_context(request, "rel-types", form=form, kind=kind, is_p2p=is_p2p, mode="create")
    return render(request, "setup/rel_type_form.html", ctx)


@owner_required
def rel_type_edit(request, kind, pk):
    if kind not in REL_KINDS:
        raise Http404()
    model, form_class, is_p2p = REL_KINDS[kind]
    obj = get_object_or_404(model, pk=pk)
    if obj.is_system:
        return HttpResponseForbidden("System relationship types are locked and cannot be edited.")

    form = form_class(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect(setup_url(request, "relationship-types/"))

    ctx = setup_context(
        request, "rel-types", form=form, kind=kind, is_p2p=is_p2p, obj=obj, mode="edit"
    )
    return render(request, "setup/rel_type_form.html", ctx)


@owner_required
def rel_type_delete(request, kind, pk):
    if kind not in REL_KINDS:
        raise Http404()
    model, _form_class, _is_p2p = REL_KINDS[kind]
    obj = get_object_or_404(model, pk=pk)
    if obj.is_system:
        return HttpResponseForbidden("System relationship types are locked and cannot be deleted.")
    if request.method == "POST":
        obj.delete()  # hard delete: no dependents in P3 (relationship edges land in P5)
    return redirect(setup_url(request, "relationship-types/"))


# --- Members & invitations ------------------------------------------------------------------
# Full management (DESIGN §4/§8): roster, invite, revoke/resend, role change, remove — all scoped
# to request.tenant. A last-Owner guard prevents removing/demoting the final Owner (server-side).


def _send_invitation_email(request, invitation):
    accept_url = request.build_absolute_uri(invitation.get_accept_path())
    send_mail(
        subject=f"You're invited to {request.tenant.name} on MyNestra",
        message=(
            f"You've been invited to join {request.tenant.name}.\n\n"
            f"Accept your invitation:\n{accept_url}\n\n"
            f"This link expires on {invitation.expires_at:%d-%b-%Y}."
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[invitation.email],
    )


def _owner_count(tenant):
    return Membership.objects.filter(tenant=tenant, role=Role.OWNER).count()


def _render_members(request, invite_form):
    memberships = (
        Membership.objects.filter(tenant=request.tenant)
        .select_related("user")
        .order_by("-role", "joined_at")
    )
    invitations = Invitation.objects.filter(tenant=request.tenant).order_by("-created_at")
    ctx = setup_context(
        request, "members",
        memberships=memberships,
        invitations=invitations,
        owner_count=_owner_count(request.tenant),
        invite_form=invite_form,
    )
    return render(request, "setup/members.html", ctx)


@owner_required
def members(request):
    return _render_members(request, InviteForm())


@owner_required
def member_invite(request):
    if request.method != "POST":
        return redirect(setup_url(request, "members/"))
    form = InviteForm(request.POST)
    if not form.is_valid():
        return _render_members(request, form)

    invitation = Invitation.objects.create(
        email=form.cleaned_data["email"],
        tenant=request.tenant,
        role=form.cleaned_data["role"],
        invited_by=request.user,
    )
    _send_invitation_email(request, invitation)
    return redirect(setup_url(request, "members/"))


@owner_required
def invitation_revoke(request, pk):
    invitation = get_object_or_404(Invitation, pk=pk, tenant=request.tenant)
    if request.method == "POST" and invitation.status == Invitation.Status.PENDING:
        invitation.status = Invitation.Status.REVOKED
        invitation.save(update_fields=["status"])
    return redirect(setup_url(request, "members/"))


@owner_required
def invitation_resend(request, pk):
    invitation = get_object_or_404(Invitation, pk=pk, tenant=request.tenant)
    if request.method == "POST" and invitation.status == Invitation.Status.PENDING:
        invitation.expires_at = timezone.now() + timedelta(days=7)
        invitation.save(update_fields=["expires_at"])
        _send_invitation_email(request, invitation)
    return redirect(setup_url(request, "members/"))


@owner_required
def member_role(request, pk):
    membership = get_object_or_404(Membership, pk=pk, tenant=request.tenant)
    new_role = request.POST.get("role")
    if request.method != "POST" or new_role not in (Role.OWNER, Role.MEMBER):
        return redirect(setup_url(request, "members/"))
    # Last-owner guard: never demote the final Owner.
    demoting_owner = membership.role == Role.OWNER and new_role == Role.MEMBER
    if demoting_owner and _owner_count(request.tenant) <= 1:
        return HttpResponseForbidden("The household must keep at least one owner.")
    membership.role = new_role
    membership.save(update_fields=["role"])
    return redirect(setup_url(request, "members/"))


@owner_required
def member_remove(request, pk):
    membership = get_object_or_404(Membership, pk=pk, tenant=request.tenant)
    # Last-owner guard: never remove the final Owner.
    if membership.role == Role.OWNER and _owner_count(request.tenant) <= 1:
        return HttpResponseForbidden("The household must keep at least one owner.")
    if request.method == "POST":
        membership.delete()
    return redirect(setup_url(request, "members/"))


# --- Appearance -----------------------------------------------------------------------------
# Household palette (Tenant.palette, owner-set, recolors everyone) + per-user theme (User.theme).
# Both are server-authoritative (see apps.core.context_processors.ui + base.html pre-paint).


@owner_required
def appearance(request):
    tenant = request.tenant
    from apps.users.models import User

    if request.method == "POST":
        palette = request.POST.get("palette")
        if palette in Tenant.Palette.values:
            # queryset update avoids TenantMixin.save() (which would switch the connection schema).
            Tenant.objects.filter(pk=tenant.pk).update(palette=palette)
        theme = request.POST.get("theme", "")
        request.user.theme = theme if theme in User.Theme.values else None
        request.user.save(update_fields=["theme"])
        return redirect(setup_url(request, "appearance/"))

    ctx = setup_context(
        request, "appearance",
        palette=tenant.palette,
        theme=request.user.theme or "",
    )
    return render(request, "setup/appearance.html", ctx)
