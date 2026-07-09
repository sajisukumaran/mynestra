"""Organizations views (tenant-scoped, member-accessible). Mirrors the Contacts idiom (P4/P5):
list search/filter-chip/sort/paginate, a create/edit form with Alpine-managed inline channels +
identifiers parsed server-side, and a tabbed detail with slide-over CRUD. No mockup — composes the
existing kit."""

from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render

from apps.contacts.forms import AddressForm
from apps.contacts.models import Address, ContactChannel, Person
from apps.organizations.forms import BranchForm, OrganizationForm
from apps.organizations.models import Branch, Organization, OrgIdentifier
from apps.relationships.models import PersonOrgRelationship, PersonOrgRelationshipType
from apps.relationships.services import parse_partial_dates
from apps.setup.models import Category
from apps.tenants.models import Membership, Role

SORTS = {
    "name": ("name", "id"),
    "-name": ("-name", "-id"),
    "added": ("created_at", "id"),
    "-added": ("-created_at", "-id"),
}


def tenant_url(request, path=""):
    return f"/t/{request.tenant.schema_name}/{path}"


def _is_owner(request) -> bool:
    return Membership.objects.filter(
        user=request.user, tenant=request.tenant, role=Role.OWNER
    ).exists()


def org_context(request, active, **extra):
    ctx = {
        "active": active,
        "is_owner": _is_owner(request),
        "nav_orgs": Organization.objects.count(),
    }
    ctx.update(extra)
    return ctx


def org_dashboard(request):
    """Organizations dashboard (DESIGN §8): counts, a by-category breakdown (the Bank seam stays
    visible), and recents. No mockup — composes the kit in the Contacts idiom."""
    import datetime

    from django.utils import timezone

    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    org_alive = Q(organizations__deleted_at__isnull=True)
    categories = (
        Category.objects.filter(kind=Category.Kind.ORG)
        .annotate(n=Count("organizations", filter=org_alive, distinct=True))
        .filter(n__gt=0)
        .order_by("-n", "name")
    )
    recent_updated = [
        o
        for o in Organization.objects.order_by("-updated_at")[:15]
        if (o.updated_at - o.created_at).total_seconds() >= 2
    ][:5]
    ctx = org_context(
        request, "dashboard",
        orgs_count=Organization.objects.count(),
        branches_count=Branch.objects.count(),
        key_people=PersonOrgRelationship.objects.values("person").distinct().count(),
        added_this_month=Organization.objects.filter(created_at__gte=month_start).count(),
        recent_count=Organization.objects.filter(
            created_at__gte=now - datetime.timedelta(days=7)
        ).count(),
        categories=categories,
        recent_added=list(Organization.objects.order_by("-created_at")[:5]),
        recent_updated=recent_updated,
    )
    return render(request, "organizations/dashboard.html", ctx)


def org_list(request):
    qs = Organization.objects.prefetch_related("categories", "channels", "addresses")

    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(name__icontains=q) | Q(display_name__icontains=q) | Q(channels__value__icontains=q)
        ).distinct()

    category = request.GET.get("category", "")
    if category.isdigit():
        qs = qs.filter(categories__id=category)

    sort = request.GET.get("sort", "name")
    if sort not in SORTS:
        sort = "name"
    qs = qs.order_by(*SORTS[sort])

    categories = Category.objects.filter(kind=Category.Kind.ORG).annotate(
        n=Count("organizations", filter=Q(organizations__deleted_at__isnull=True), distinct=True)
    )
    total = Organization.objects.count()
    page = Paginator(qs, 10).get_page(request.GET.get("page"))

    ctx = org_context(
        request, "organizations",
        page=page, orgs=page.object_list, q=q, category=category, sort=sort,
        sort_name_next="-name" if sort == "name" else "name",
        sort_added_next="-added" if sort == "added" else "added",
        categories=categories, total=total,
    )
    return render(request, "organizations/org_list.html", ctx)


# --- Create / edit --------------------------------------------------------------------------

def _save_channels(request, org):
    types = request.POST.getlist("channel_type")
    values = request.POST.getlist("channel_value")
    labels = request.POST.getlist("channel_label")
    primaries = request.POST.getlist("channel_primary")
    org.channels.all().delete()
    for ctype, value, label, primary in zip(types, values, labels, primaries, strict=False):
        value = value.strip()
        if value:
            ContactChannel.objects.create(
                organization=org, type=ctype, value=value, label=label.strip(),
                is_primary=(primary == "1"),
            )


def _save_identifiers(request, org):
    org_types = request.POST.getlist("identifier_type")
    values = request.POST.getlist("identifier_value")
    org.identifiers.all().delete()
    for itype, value in zip(org_types, values, strict=False):
        itype, value = itype.strip(), value.strip()
        if itype and value:
            OrgIdentifier.objects.create(organization=org, type=itype, value=value)


def org_create(request):
    return _org_form(request, Organization(), "create")


def org_edit(request, pk):
    return _org_form(request, get_object_or_404(Organization, pk=pk), "edit")


def _org_form(request, org, mode):
    form = OrganizationForm(request.POST or None, request.FILES or None, instance=org)
    if request.method == "POST" and form.is_valid():
        org = form.save(commit=False)
        for field, value in parse_partial_dates(request.POST, "established", "closed").items():
            setattr(org, field, value)
        org.save()
        _save_channels(request, org)
        _save_identifiers(request, org)
        org.categories.set(request.POST.getlist("categories"))
        return redirect(tenant_url(request, f"organizations/{org.pk}/"))

    channels_data = [
        {"type": c.type, "value": c.value, "label": c.label, "primary": c.is_primary}
        for c in (org.channels.all() if org.pk else [])
    ]
    identifiers_data = [
        {"type": i.type, "value": i.value} for i in (org.identifiers.all() if org.pk else [])
    ]
    ctx = org_context(
        request, "organizations",
        form=form, org=org, mode=mode,
        channels_data=channels_data, identifiers_data=identifiers_data,
        all_categories=Category.objects.filter(kind=Category.Kind.ORG),
        selected_category_ids={str(i) for i in org.categories.values_list("id", flat=True)}
        if org.pk else set(),
    )
    return render(request, "organizations/org_form.html", ctx)


def org_delete(request, pk):
    org = get_object_or_404(Organization, pk=pk)
    if request.method == "POST":
        org.delete()  # soft-delete → Setup → Recently deleted
    return redirect(tenant_url(request, "organizations/all/"))


# --- Detail (Overview / Branches / Key people / History) ------------------------------------

def _org_qs():
    return Organization.objects.prefetch_related(
        "categories", "channels", "addresses", "identifiers",
        "branches__channels", "branches__addresses",
        "people_links__type", "people_links__person__categories",
    )


def _key_people_rows(org):
    """P2O links for this org: person + role label + optional 'since' date."""
    return [
        {
            "link": e,
            "person": e.person,
            "role": e.type.label,
            "since": e.from_date.display,
        }
        for e in org.people_links.all()
    ]


def org_detail(request, pk):
    return _render_detail(request, get_object_or_404(_org_qs(), pk=pk))


def _render_detail(request, org, address_form=None, reopen=""):
    branches = list(org.branches.all())
    ctx = org_context(
        request, "organizations",
        org=org,
        branches=branches,
        branch_count=len(branches),
        key_people=_key_people_rows(org),
        p2o_types=PersonOrgRelationshipType.objects.all(),
        history=org.history.all()[:60],
        address_form=address_form or AddressForm(),
        reopen=reopen,
    )
    return render(request, "organizations/org_detail.html", ctx)


# --- Org-owned addresses (edited from the detail via slide-over) -----------------------------

def org_address_create(request, pk):
    org = get_object_or_404(Organization, pk=pk)
    if request.method == "POST":
        form = AddressForm(request.POST)
        if form.is_valid():
            address = form.save(commit=False)
            address.organization = org
            address.save()
        else:
            return _render_detail(request, org, address_form=form, reopen="address")
    return redirect(tenant_url(request, f"organizations/{pk}/"))


def org_address_edit(request, pk, addr_pk):
    org = get_object_or_404(Organization, pk=pk)
    address = get_object_or_404(Address, pk=addr_pk, organization=org)
    if request.method == "POST":
        AddressForm(request.POST, instance=address).save()
    return redirect(tenant_url(request, f"organizations/{pk}/"))


def org_address_delete(request, pk, addr_pk):
    org = get_object_or_404(Organization, pk=pk)
    if request.method == "POST":
        Address.objects.filter(pk=addr_pk, organization=org).delete()
    return redirect(tenant_url(request, f"organizations/{pk}/"))


# --- Branches (managed on the org detail Branches tab; each owns its channels/addresses) -----

_BRANCH_ADDR_FIELDS = ("label", "line1", "line2", "city", "region", "postal_code", "country")


def _upsert_branch_primary_address(request, branch):
    """Create/update the branch's primary Address from the address fields folded into the branch
    popup. No-op when every field is blank; extra (non-primary) addresses are left untouched."""
    data = {f: request.POST.get(f, "").strip() for f in _BRANCH_ADDR_FIELDS}
    if not any(data.values()):
        return
    address = branch.addresses.filter(is_primary=True).first()
    if address:
        for field, value in data.items():
            setattr(address, field, value)
        address.save()
    else:
        Address.objects.create(branch=branch, is_primary=True, **data)


def _set_branch_dates(request, branch):
    for field, value in parse_partial_dates(request.POST, "opened", "closed").items():
        setattr(branch, field, value)


def branch_create(request, pk):
    org = get_object_or_404(Organization, pk=pk)
    if request.method == "POST":
        form = BranchForm(request.POST)
        if form.is_valid():
            branch = form.save(commit=False)
            branch.organization = org
            _set_branch_dates(request, branch)
            branch.save()
            _upsert_branch_primary_address(request, branch)
    return redirect(tenant_url(request, f"organizations/{pk}/"))


def branch_edit(request, pk, branch_pk):
    branch = get_object_or_404(Branch, pk=branch_pk, organization_id=pk)
    if request.method == "POST":
        form = BranchForm(request.POST, instance=branch)
        if form.is_valid():
            branch = form.save(commit=False)
            _set_branch_dates(request, branch)
            branch.save()
            _upsert_branch_primary_address(request, branch)
    return redirect(tenant_url(request, f"organizations/{pk}/"))


def branch_delete(request, pk, branch_pk):
    if request.method == "POST":
        get_object_or_404(Branch, pk=branch_pk, organization_id=pk).delete()  # soft
    return redirect(tenant_url(request, f"organizations/{pk}/"))


def _channel_kwargs(request):
    return {
        "type": request.POST.get("type", "phone"),
        "value": request.POST.get("value", "").strip(),
        "label": request.POST.get("label", "").strip(),
        "is_primary": request.POST.get("is_primary") in ("on", "1", "true"),
    }


def branch_channel_create(request, pk, branch_pk):
    branch = get_object_or_404(Branch, pk=branch_pk, organization_id=pk)
    if request.method == "POST":
        kw = _channel_kwargs(request)
        if kw["value"]:
            ContactChannel.objects.create(branch=branch, **kw)
    return redirect(tenant_url(request, f"organizations/{pk}/"))


def branch_channel_edit(request, pk, branch_pk, ch_pk):
    channel = get_object_or_404(
        ContactChannel, pk=ch_pk, branch__pk=branch_pk, branch__organization_id=pk
    )
    if request.method == "POST":
        for field, value in _channel_kwargs(request).items():
            setattr(channel, field, value)
        channel.save()
    return redirect(tenant_url(request, f"organizations/{pk}/"))


def branch_channel_delete(request, pk, branch_pk, ch_pk):
    if request.method == "POST":
        ContactChannel.objects.filter(
            pk=ch_pk, branch__pk=branch_pk, branch__organization_id=pk
        ).delete()
    return redirect(tenant_url(request, f"organizations/{pk}/"))


def branch_address_create(request, pk, branch_pk):
    branch = get_object_or_404(Branch, pk=branch_pk, organization_id=pk)
    if request.method == "POST":
        form = AddressForm(request.POST)
        if form.is_valid():
            address = form.save(commit=False)
            address.branch = branch
            address.save()
    return redirect(tenant_url(request, f"organizations/{pk}/"))


def branch_address_edit(request, pk, branch_pk, addr_pk):
    address = get_object_or_404(
        Address, pk=addr_pk, branch__pk=branch_pk, branch__organization_id=pk
    )
    if request.method == "POST":
        AddressForm(request.POST, instance=address).save()
    return redirect(tenant_url(request, f"organizations/{pk}/"))


def branch_address_delete(request, pk, branch_pk, addr_pk):
    if request.method == "POST":
        Address.objects.filter(
            pk=addr_pk, branch__pk=branch_pk, branch__organization_id=pk
        ).delete()
    return redirect(tenant_url(request, f"organizations/{pk}/"))


# --- Key people (P2O), managed from the org detail ------------------------------------------

def org_person_search(request, pk):
    """htmx: people not yet linked to this org (for the key-people picker)."""
    org = get_object_or_404(Organization, pk=pk)
    linked = set(org.people_links.values_list("person_id", flat=True))
    qs = Person.objects.exclude(pk__in=linked)
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(first_name__icontains=q) | Q(last_name__icontains=q)
            | Q(preferred_name__icontains=q)
        )
    return render(request, "contacts/partials/rel_search.html", {"candidates": qs[:8], "q": q})


def p2o_create(request, pk):
    org = get_object_or_404(Organization, pk=pk)
    if request.method == "POST":
        person = Person.objects.filter(pk=request.POST.get("person", "")).first()
        rtype = PersonOrgRelationshipType.objects.filter(pk=request.POST.get("type", "")).first()
        if person and rtype:
            exists = PersonOrgRelationship.objects.filter(
                person=person, organization=org, type=rtype
            ).exists()
            if not exists:
                PersonOrgRelationship.objects.create(
                    person=person, organization=org, type=rtype,
                    role_note=request.POST.get("role_note", "").strip(),
                    **parse_partial_dates(request.POST, "from", "to"),
                )
    return redirect(tenant_url(request, f"organizations/{pk}/"))


def p2o_edit(request, pk, link_pk):
    link = get_object_or_404(PersonOrgRelationship, pk=link_pk, organization_id=pk)
    if request.method == "POST":
        rtype = PersonOrgRelationshipType.objects.filter(pk=request.POST.get("type", "")).first()
        if rtype:
            link.type = rtype
            link.role_note = request.POST.get("role_note", "").strip()
            for field, value in parse_partial_dates(request.POST, "from", "to").items():
                setattr(link, field, value)
            link.save()
    return redirect(tenant_url(request, f"organizations/{pk}/"))


def p2o_delete(request, pk, link_pk):
    if request.method == "POST":
        PersonOrgRelationship.objects.filter(pk=link_pk, organization_id=pk).delete()
    return redirect(tenant_url(request, f"organizations/{pk}/"))
