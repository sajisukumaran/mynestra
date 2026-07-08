"""Contacts views (tenant-scoped, member-accessible; MembershipMiddleware enforces access).

P4 delivers the People list, detail (Overview + History) and create/edit form. Feature templates
compose cotton components only; the Contacts shell/sidebar wraps every page.
"""

from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import render

from apps.contacts.models import Person
from apps.setup.models import Category
from apps.tenants.models import Membership, Role

# Sort keys → order_by tuples for the People list column headers.
SORTS = {
    "name": ("first_name", "last_name"),
    "-name": ("-first_name", "-last_name"),
    "added": ("created_at", "id"),
    "-added": ("-created_at", "-id"),
}


def _is_owner(request) -> bool:
    return Membership.objects.filter(
        user=request.user, tenant=request.tenant, role=Role.OWNER
    ).exists()


def contacts_context(request, active, **extra):
    """Shared context for every Contacts page: Contacts sidebar state + owner flag."""
    ctx = {
        "active": active,
        "is_owner": _is_owner(request),
        "nav_people": Person.objects.count(),
    }
    ctx.update(extra)
    return ctx


def people_list(request):
    qs = Person.objects.prefetch_related("categories", "channels", "addresses")

    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(first_name__icontains=q)
            | Q(last_name__icontains=q)
            | Q(preferred_name__icontains=q)
            | Q(channels__value__icontains=q)
        ).distinct()

    category = request.GET.get("category", "")
    if category.isdigit():
        qs = qs.filter(categories__id=category)

    sort = request.GET.get("sort", "name")
    if sort not in SORTS:
        sort = "name"
    qs = qs.order_by(*SORTS[sort])

    categories = Category.objects.filter(kind=Category.Kind.PERSON).annotate(
        n=Count("people", filter=Q(people__deleted_at__isnull=True), distinct=True)
    )
    total = Person.objects.count()

    page = Paginator(qs, 10).get_page(request.GET.get("page"))

    ctx = contacts_context(
        request, "people",
        page=page,
        people=page.object_list,
        q=q,
        category=category,
        sort=sort,
        sort_name_next="-name" if sort == "name" else "name",
        sort_added_next="-added" if sort == "added" else "added",
        categories=categories,
        total=total,
    )
    return render(request, "contacts/people_list.html", ctx)
