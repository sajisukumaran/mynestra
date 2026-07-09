"""Contacts views (tenant-scoped, member-accessible; MembershipMiddleware enforces access).

P4 delivers the People list, detail (Overview + History) and create/edit form. Feature templates
compose cotton components only; the Contacts shell/sidebar wraps every page.
"""

from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render

from apps.contacts.forms import AddressForm, ImportantDateForm, PersonForm
from apps.contacts.models import Address, ContactChannel, ImportantDate, Person
from apps.relationships.models import PersonRelationship, RelationshipType
from apps.relationships.services import label_for, other_side
from apps.setup.models import Category
from apps.tenants.models import Membership, Role


def tenant_url(request, path=""):
    return f"/t/{request.tenant.schema_name}/{path}"


def contacts_home(request):
    """Contacts landing → People (the only live section in P4)."""
    return redirect(tenant_url(request, "contacts/people/"))

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


# --- Create / edit --------------------------------------------------------------------------

def person_create(request):
    return _person_form(request, Person(), "create")


def person_edit(request, pk):
    return _person_form(request, get_object_or_404(Person, pk=pk), "edit")


def _save_channels(request, person):
    """Rebuild channels from the Alpine-managed parallel arrays (empty-value rows are skipped)."""
    types = request.POST.getlist("channel_type")
    values = request.POST.getlist("channel_value")
    labels = request.POST.getlist("channel_label")
    primaries = request.POST.getlist("channel_primary")
    person.channels.all().delete()
    for ctype, value, label, primary in zip(types, values, labels, primaries, strict=False):
        value = value.strip()
        if value:
            ContactChannel.objects.create(
                person=person, type=ctype, value=value, label=label.strip(),
                is_primary=(primary == "1"),
            )


def _person_form(request, person, mode):
    form = PersonForm(request.POST or None, request.FILES or None, instance=person)
    if request.method == "POST" and form.is_valid():
        person = form.save()
        _save_channels(request, person)
        person.categories.set(request.POST.getlist("categories"))
        return redirect(tenant_url(request, f"contacts/people/{person.pk}/"))

    existing = person.channels.all() if person.pk else []
    channels_data = [
        {"type": c.type, "value": c.value, "label": c.label, "primary": c.is_primary}
        for c in existing
    ]
    ctx = contacts_context(
        request, "people",
        form=form, person=person, mode=mode,
        channels_data=channels_data,
        all_categories=Category.objects.filter(kind=Category.Kind.PERSON),
        selected_category_ids={str(i) for i in person.categories.values_list("id", flat=True)}
        if person.pk else set(),
    )
    return render(request, "contacts/person_form.html", ctx)


# --- Relationships & families helpers --------------------------------------------------------

def _rel_rows(person):
    """This person's P2P relationships, each resolved for display on *their* page: the row shows
    the other endpoint and the label describing that endpoint (by its own gender)."""
    edges = (
        PersonRelationship.objects.filter(Q(person_a=person) | Q(person_b=person))
        .select_related("type", "person_a", "person_b")
    )
    rows = [
        {
            "rel": e,
            "other": e.other_of(person),
            "role": e.label_for_other(person),
            "type_name": e.type.display_name,
            "selected_value": f"{e.type_id}:{e.side_of(person)}",  # preselect on the edit form
        }
        for e in edges
    ]
    rows.sort(key=lambda r: r["other"].display_name.lower())
    return rows


def _directed_type_options(types):
    """Options for the add/edit relationship <select>. Value is '<type_id>:<side>' where side is
    the *subject's* side. Symmetric types → one option (the neutral pairing label); asymmetric →
    two directed options labelled by the subject-side neutral label (e.g. Parent / Child)."""
    opts = []
    for t in types:
        opts.append({"value": f"{t.pk}:a", "label": t.a_label_n})
        if not t.is_symmetric:
            opts.append({"value": f"{t.pk}:b", "label": t.b_label_n})
    return opts


def _family_cards(person):
    """Families this person belongs to, with a small member-avatar preview for the cards."""
    cards = []
    for fam in person.families.all():
        members = list(fam.members.all())
        cards.append({
            "family": fam,
            "members": members[:5],
            "more": max(0, len(members) - 5),
            "count": len(members),
        })
    return cards


def _parse_typeside(raw):
    """'12:a' -> (RelationshipType|None, 'a'|'b'|None)."""
    type_id, _, side = raw.partition(":")
    if side not in ("a", "b") or not type_id.isdigit():
        return None, None
    return RelationshipType.objects.filter(pk=type_id).first(), side


# --- Detail (Overview + History) ------------------------------------------------------------

def _person_qs():
    return Person.objects.prefetch_related(
        "categories", "channels", "addresses", "important_dates"
    )


def person_detail(request, pk):
    return _render_detail(request, get_object_or_404(_person_qs(), pk=pk))


def person_delete(request, pk):
    """Soft-delete a person (member-level); it moves to Setup → Recently deleted for restore."""
    person = get_object_or_404(Person, pk=pk)
    if request.method == "POST":
        person.delete()
    return redirect(tenant_url(request, "contacts/people/"))


def _render_detail(request, person, address_form=None, date_form=None, reopen=""):
    rel_rows = _rel_rows(person)
    family_cards = _family_cards(person)
    ctx = contacts_context(
        request, "people",
        person=person,
        history=person.history.all()[:60],
        address_form=address_form or AddressForm(),
        date_form=date_form or ImportantDateForm(),
        reopen=reopen,
        rel_rows=rel_rows,
        rel_count=len(rel_rows),
        family_cards=family_cards,
        family_count=len(family_cards),
        rel_type_options=_directed_type_options(RelationshipType.objects.all()),
    )
    return render(request, "contacts/person_detail.html", ctx)


# --- Person relationships (P2P), edited from the detail via modal + htmx ---------------------

def relationship_search(request, pk):
    """htmx: candidate people for the add-relationship modal (excludes self + already-related)."""
    person = get_object_or_404(Person, pk=pk)
    related_ids = {
        pid
        for e in PersonRelationship.objects.filter(Q(person_a=person) | Q(person_b=person))
        for pid in (e.person_a_id, e.person_b_id)
    }
    related_ids.add(person.pk)
    qs = Person.objects.exclude(pk__in=related_ids)
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(first_name__icontains=q) | Q(last_name__icontains=q)
            | Q(preferred_name__icontains=q)
        )
    ctx = {"person": person, "candidates": qs[:8], "q": q}
    return render(request, "contacts/partials/rel_search.html", ctx)


def relationship_preview(request, pk):
    """htmx: live gender-aware label preview for the current (other, type+side) selection."""
    person = get_object_or_404(Person, pk=pk)
    other = Person.objects.filter(pk=request.GET.get("other", "")).first()
    rel_type, side = _parse_typeside(request.GET.get("typeside", ""))
    ctx = {"person": person, "other": other}
    if other and rel_type and side:
        ctx["subject_label"] = label_for(rel_type, person.gender, side)
        ctx["other_label"] = label_for(rel_type, other.gender, other_side(side))
        ctx["ready"] = True
    return render(request, "contacts/partials/rel_preview.html", ctx)


def relationship_create(request, pk):
    person = get_object_or_404(Person, pk=pk)
    if request.method == "POST":
        other = Person.objects.filter(pk=request.POST.get("other", "")).first()
        rel_type, side = _parse_typeside(request.POST.get("typeside", ""))
        note = request.POST.get("note", "").strip()
        if other and rel_type and side and other.pk != person.pk:
            person_a, person_b = (person, other) if side == "a" else (other, person)
            already = PersonRelationship.objects.filter(
                Q(person_a=person_a, person_b=person_b)
                | Q(person_a=person_b, person_b=person_a),
                type=rel_type,
            ).exists()
            if not already:
                PersonRelationship.objects.create(
                    person_a=person_a, person_b=person_b, type=rel_type, note=note
                )
    return redirect(tenant_url(request, f"contacts/people/{pk}/"))


def relationship_edit(request, pk, rel_pk):
    """Change an existing edge's type/direction + note (the pair itself is fixed)."""
    person = get_object_or_404(Person, pk=pk)
    rel = get_object_or_404(
        PersonRelationship.objects.filter(Q(person_a=person) | Q(person_b=person)), pk=rel_pk
    )
    if request.method == "POST":
        other = rel.other_of(person)
        rel_type, side = _parse_typeside(request.POST.get("typeside", ""))
        if rel_type and side:
            rel.person_a, rel.person_b = (person, other) if side == "a" else (other, person)
            rel.type = rel_type
            rel.note = request.POST.get("note", "").strip()
            rel.save()
    return redirect(tenant_url(request, f"contacts/people/{pk}/"))


def relationship_delete(request, pk, rel_pk):
    person = get_object_or_404(Person, pk=pk)
    if request.method == "POST":
        rel = (
            PersonRelationship.objects.filter(Q(person_a=person) | Q(person_b=person))
            .filter(pk=rel_pk)
            .first()
        )
        if rel:
            rel.delete()  # soft-delete
    return redirect(tenant_url(request, f"contacts/people/{pk}/"))


# --- Addresses & important dates (edited from the detail via slide-over) ---------------------

def address_create(request, pk):
    person = get_object_or_404(Person, pk=pk)
    if request.method == "POST":
        form = AddressForm(request.POST)
        if form.is_valid():
            address = form.save(commit=False)
            address.person = person
            address.save()
        else:
            return _render_detail(request, person, address_form=form, reopen="address")
    return redirect(tenant_url(request, f"contacts/people/{pk}/"))


def address_edit(request, pk, addr_pk):
    person = get_object_or_404(Person, pk=pk)
    address = get_object_or_404(Address, pk=addr_pk, person=person)
    if request.method == "POST":
        AddressForm(request.POST, instance=address).save()
    return redirect(tenant_url(request, f"contacts/people/{pk}/"))


def address_delete(request, pk, addr_pk):
    person = get_object_or_404(Person, pk=pk)
    if request.method == "POST":
        Address.objects.filter(pk=addr_pk, person=person).delete()
    return redirect(tenant_url(request, f"contacts/people/{pk}/"))


def importantdate_create(request, pk):
    person = get_object_or_404(Person, pk=pk)
    if request.method == "POST":
        form = ImportantDateForm(request.POST)
        if form.is_valid():
            date = form.save(commit=False)
            date.person = person
            date.save()
        else:
            return _render_detail(request, person, date_form=form, reopen="date")
    return redirect(tenant_url(request, f"contacts/people/{pk}/"))


def importantdate_edit(request, pk, date_pk):
    person = get_object_or_404(Person, pk=pk)
    date = get_object_or_404(ImportantDate, pk=date_pk, person=person)
    if request.method == "POST":
        form = ImportantDateForm(request.POST, instance=date)
        if form.is_valid():
            form.save()
    return redirect(tenant_url(request, f"contacts/people/{pk}/"))


def importantdate_delete(request, pk, date_pk):
    person = get_object_or_404(Person, pk=pk)
    if request.method == "POST":
        ImportantDate.objects.filter(pk=date_pk, person=person).delete()
    return redirect(tenant_url(request, f"contacts/people/{pk}/"))
