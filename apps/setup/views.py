"""Setup app views (tenant-scoped, Owner-only).

The Setup surface manages the household's catalogs and settings (DESIGN §8): categories,
relationship types, members & invitations, appearance, tenant profile, recently-deleted. Every
view is guarded by `owner_required`; feature templates compose cotton components only.
"""

from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.db.models import Q
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.accounts.decorators import owner_required
from apps.contacts.models import Person
from apps.families.models import Family
from apps.finance.models import Currency
from apps.organizations.models import Branch, Organization
from apps.payables.forms import PaymentTermForm
from apps.payables.models import PaymentTerm
from apps.relationships.models import PersonOrgRelationshipType, RelationshipType
from apps.setup.forms import (
    CategoryForm,
    InviteForm,
    PersonOrgRelationshipTypeForm,
    RelationshipTypeForm,
)
from apps.setup.models import Category
from apps.tenants.models import CURATED_TIMEZONES, Invitation, Membership, Role, Tenant

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
        "nav_payment_terms": PaymentTerm.objects.count(),
        "nav_members": Membership.objects.filter(tenant=request.tenant).count(),
        "nav_household_members": Person.objects.filter(is_household_member=True).count(),
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


# --- Payment terms (Payables catalog) -------------------------------------------------------
# System rows (seeded §6) are locked like categories: edit/delete refused server-side.


@owner_required
def payment_terms(request):
    """List payment terms; system rows show a lock, custom rows can be edited/deleted."""
    ctx = setup_context(request, "payment-terms", terms=PaymentTerm.objects.all())
    return render(request, "setup/payment_terms.html", ctx)


@owner_required
def payment_term_create(request):
    form = PaymentTermForm(request.POST or None, instance=PaymentTerm())
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect(setup_url(request, "payment-terms/"))
    ctx = setup_context(request, "payment-terms", form=form, mode="create")
    return render(request, "setup/payment_term_form.html", ctx)


@owner_required
def payment_term_edit(request, pk):
    term = get_object_or_404(PaymentTerm, pk=pk)
    if term.is_system:
        return HttpResponseForbidden("System payment terms are locked and cannot be edited.")
    form = PaymentTermForm(request.POST or None, instance=term)
    if request.method == "POST" and form.is_valid():
        form.save()
        return redirect(setup_url(request, "payment-terms/"))
    ctx = setup_context(request, "payment-terms", form=form, term=term, mode="edit")
    return render(request, "setup/payment_term_form.html", ctx)


@owner_required
def payment_term_delete(request, pk):
    term = get_object_or_404(PaymentTerm, pk=pk)
    if term.is_system:
        return HttpResponseForbidden("System payment terms are locked and cannot be deleted.")
    if request.method == "POST":
        term.delete()  # hard delete: bills that reference a term PROTECT it (guarded in module 6)
    return redirect(setup_url(request, "payment-terms/"))


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


# --- Household members (which People belong to this household) -------------------------------

def _render_household_members(request):
    members = Person.objects.filter(is_household_member=True).order_by("first_name", "last_name")
    ctx = setup_context(request, "household-members", members=members)
    return render(request, "setup/household_members.html", ctx)


@owner_required
def household_members(request):
    return _render_household_members(request)


@owner_required
def household_member_search(request):
    """htmx: People not yet in the household, matching the query (candidates to add)."""
    qs = Person.objects.filter(is_household_member=False)
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(preferred_name__icontains=q)
        )
    return render(request, "setup/partials/household_search.html", {"candidates": qs[:8], "q": q})


@owner_required
def household_member_add(request):
    if request.method == "POST":
        person = Person.objects.filter(pk=request.POST.get("person") or 0).first()
        if person and not person.is_household_member:
            person.is_household_member = True
            person.save(update_fields=["is_household_member", "updated_at"])
    return redirect(setup_url(request, "household-members/"))


@owner_required
def household_member_remove(request, pk):
    if request.method == "POST":
        person = get_object_or_404(Person, pk=pk)
        person.is_household_member = False
        person.save(update_fields=["is_household_member", "updated_at"])
    return redirect(setup_url(request, "household-members/"))


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


# --- Localization ---------------------------------------------------------------------------
# Household default currency (the finance base/functional currency) + timezone + date/number
# formats. Tenant scalars are written via queryset update (no TenantMixin.save() schema switch);
# currency is validated against the tenant-schema Currency catalog. Exposed to templates via the
# `ui` context processor for money/date formatting.


@owner_required
def localization(request):
    tenant = request.tenant
    if request.method == "POST":
        code = request.POST.get("currency")
        if code and Currency.objects.filter(code=code, is_active=True).exists():
            Tenant.objects.filter(pk=tenant.pk).update(currency=code)
        tz = request.POST.get("timezone")
        if tz in CURATED_TIMEZONES:
            Tenant.objects.filter(pk=tenant.pk).update(timezone=tz)
        date_format = request.POST.get("date_format")
        if date_format in Tenant.DateFormat.values:
            Tenant.objects.filter(pk=tenant.pk).update(date_format=date_format)
        number_format = request.POST.get("number_format")
        if number_format in Tenant.NumberFormat.values:
            Tenant.objects.filter(pk=tenant.pk).update(number_format=number_format)
        return redirect(setup_url(request, "localization/"))

    ctx = setup_context(
        request, "localization",
        currencies=Currency.objects.filter(is_active=True).order_by("code"),
        currency=tenant.currency,
        timezone=tenant.timezone,
        timezones=CURATED_TIMEZONES,
        date_format=tenant.date_format,
        number_format=tenant.number_format,
    )
    return render(request, "setup/localization.html", ctx)


# --- Accounting mode --------------------------------------------------------------------------
# Standard (default): the double-entry GL is invisible; the software picks every account. Expert:
# the household controls the Chart of Accounts + per-account posting maps (see DESIGN / plan).
# The switch is Standard->Expert freely; Expert->Standard only while `accounting_locked` is False
# (it flips True on the first Standard-critical COA edit, done in the finance COA editor). All
# writes go through a queryset update to avoid TenantMixin.save() (which switches schema).


@owner_required
def mode(request):
    tenant = request.tenant
    if request.method == "POST":
        target = request.POST.get("mode")
        if target == Tenant.AccountingMode.EXPERT:
            Tenant.objects.filter(pk=tenant.pk).update(
                accounting_mode=Tenant.AccountingMode.EXPERT
            )
        elif target == Tenant.AccountingMode.STANDARD and not tenant.accounting_locked:
            # Reverting to Standard is allowed only while the seeded COA is still pristine.
            Tenant.objects.filter(pk=tenant.pk).update(
                accounting_mode=Tenant.AccountingMode.STANDARD
            )
        return redirect(setup_url(request, "mode/"))

    ctx = setup_context(
        request, "mode",
        mode=tenant.accounting_mode,
        is_expert=tenant.accounting_mode == Tenant.AccountingMode.EXPERT,
        locked=tenant.accounting_locked,
    )
    return render(request, "setup/mode.html", ctx)


# --- Household profile ----------------------------------------------------------------------
# Tenant is a public (django-tenants) model; name is updated via queryset (no schema switch), the
# logo via model save (TenantMixin.save() resets to public — safe because we redirect after).


@owner_required
def profile(request):
    tenant = request.tenant
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        if name:
            Tenant.objects.filter(pk=tenant.pk).update(name=name)
        if request.POST.get("remove_logo") and tenant.logo:
            tenant.logo.delete(save=False)
            Tenant.objects.filter(pk=tenant.pk).update(logo="")
        elif request.FILES.get("logo"):
            tenant.logo = request.FILES["logo"]
            tenant.save(update_fields=["logo"])
        return redirect(setup_url(request, "profile/"))

    return render(request, "setup/profile.html", setup_context(request, "profile", tenant=tenant))


# --- Recently deleted -----------------------------------------------------------------------
# Lists soft-deleted records with Restore. Permanent (hard) delete is gated by ALLOW_HARD_DELETE
# (dev=1); prod (=0) hides it so removal only happens from here with explicit confirmation later.


@owner_required
def recently_deleted(request):
    deleted_people = Person.all_objects.filter(deleted_at__isnull=False).order_by("-deleted_at")
    deleted_families = Family.all_objects.filter(deleted_at__isnull=False).order_by("-deleted_at")
    deleted_orgs = Organization.all_objects.filter(deleted_at__isnull=False).order_by("-deleted_at")
    deleted_branches = (
        Branch.all_objects.filter(deleted_at__isnull=False)
        .select_related("organization")
        .order_by("-deleted_at")
    )
    ctx = setup_context(
        request, "recently-deleted",
        deleted_people=deleted_people,
        deleted_families=deleted_families,
        deleted_orgs=deleted_orgs,
        deleted_branches=deleted_branches,
        allow_hard_delete=settings.ALLOW_HARD_DELETE,
    )
    return render(request, "setup/recently_deleted.html", ctx)


@owner_required
def person_restore(request, pk):
    if request.method == "POST":
        get_object_or_404(Person.all_objects, pk=pk, deleted_at__isnull=False).restore()
    return redirect(setup_url(request, "recently-deleted/"))


@owner_required
def person_hard_delete(request, pk):
    if request.method == "POST" and settings.ALLOW_HARD_DELETE:
        get_object_or_404(Person.all_objects, pk=pk, deleted_at__isnull=False).hard_delete()
    return redirect(setup_url(request, "recently-deleted/"))


@owner_required
def family_restore(request, pk):
    if request.method == "POST":
        get_object_or_404(Family.all_objects, pk=pk, deleted_at__isnull=False).restore()
    return redirect(setup_url(request, "recently-deleted/"))


@owner_required
def family_hard_delete(request, pk):
    if request.method == "POST" and settings.ALLOW_HARD_DELETE:
        get_object_or_404(Family.all_objects, pk=pk, deleted_at__isnull=False).hard_delete()
    return redirect(setup_url(request, "recently-deleted/"))


@owner_required
def org_restore(request, pk):
    if request.method == "POST":
        get_object_or_404(Organization.all_objects, pk=pk, deleted_at__isnull=False).restore()
    return redirect(setup_url(request, "recently-deleted/"))


@owner_required
def org_hard_delete(request, pk):
    if request.method == "POST" and settings.ALLOW_HARD_DELETE:
        get_object_or_404(Organization.all_objects, pk=pk, deleted_at__isnull=False).hard_delete()
    return redirect(setup_url(request, "recently-deleted/"))


@owner_required
def branch_restore(request, pk):
    if request.method == "POST":
        get_object_or_404(Branch.all_objects, pk=pk, deleted_at__isnull=False).restore()
    return redirect(setup_url(request, "recently-deleted/"))


@owner_required
def branch_hard_delete(request, pk):
    if request.method == "POST" and settings.ALLOW_HARD_DELETE:
        get_object_or_404(Branch.all_objects, pk=pk, deleted_at__isnull=False).hard_delete()
    return redirect(setup_url(request, "recently-deleted/"))
