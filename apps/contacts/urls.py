"""Contacts URLs — mounted under /t/<slug>/contacts/ (member-accessible)."""

from django.urls import path

from apps.contacts import views

app_name = "contacts"

urlpatterns = [
    path("", views.contacts_home, name="home"),
    path("people/", views.people_list, name="people"),
    path("people/new/", views.person_create, name="person-create"),
    path("people/<int:pk>/", views.person_detail, name="person-detail"),
    path("people/<int:pk>/edit/", views.person_edit, name="person-edit"),
    path("people/<int:pk>/delete/", views.person_delete, name="person-delete"),
    path("people/<int:pk>/addresses/new/", views.address_create, name="address-create"),
    path(
        "people/<int:pk>/addresses/<int:addr_pk>/edit/",
        views.address_edit, name="address-edit",
    ),
    path(
        "people/<int:pk>/addresses/<int:addr_pk>/delete/",
        views.address_delete, name="address-delete",
    ),
    path("people/<int:pk>/dates/new/", views.importantdate_create, name="date-create"),
    path("people/<int:pk>/dates/<int:date_pk>/edit/", views.importantdate_edit, name="date-edit"),
    path(
        "people/<int:pk>/dates/<int:date_pk>/delete/",
        views.importantdate_delete, name="date-delete",
    ),
    # Relationships (P2P) — modal + htmx search/preview, edited from the Person detail.
    path(
        "people/<int:pk>/relationships/search/",
        views.relationship_search, name="rel-search",
    ),
    path(
        "people/<int:pk>/relationships/preview/",
        views.relationship_preview, name="rel-preview",
    ),
    path("people/<int:pk>/relationships/new/", views.relationship_create, name="rel-create"),
    path(
        "people/<int:pk>/relationships/<int:rel_pk>/edit/",
        views.relationship_edit, name="rel-edit",
    ),
    path(
        "people/<int:pk>/relationships/<int:rel_pk>/delete/",
        views.relationship_delete, name="rel-delete",
    ),
    # Person↔Organization links (P2O)
    path("people/<int:pk>/orgs/search/", views.org_link_search, name="org-link-search"),
    path("people/<int:pk>/orgs/new/", views.org_link_create, name="org-link-create"),
    path("people/<int:pk>/orgs/<int:link_pk>/edit/", views.org_link_edit, name="org-link-edit"),
    path(
        "people/<int:pk>/orgs/<int:link_pk>/delete/",
        views.org_link_delete, name="org-link-delete",
    ),
    # Families
    path("families/", views.families_list, name="families"),
    path("families/new/", views.family_create, name="family-create"),
    path("families/<int:pk>/", views.family_detail, name="family-detail"),
    path("families/<int:pk>/edit/", views.family_edit, name="family-edit"),
    path("families/<int:pk>/delete/", views.family_delete, name="family-delete"),
    path(
        "families/<int:pk>/members/search/",
        views.family_member_search, name="family-member-search",
    ),
    path("families/<int:pk>/members/add/", views.family_member_add, name="family-member-add"),
    path(
        "families/<int:pk>/members/<int:person_pk>/remove/",
        views.family_member_remove, name="family-member-remove",
    ),
    path(
        "families/<int:pk>/addresses/new/",
        views.family_address_create, name="family-address-create",
    ),
    path(
        "families/<int:pk>/addresses/<int:addr_pk>/edit/",
        views.family_address_edit, name="family-address-edit",
    ),
    path(
        "families/<int:pk>/addresses/<int:addr_pk>/delete/",
        views.family_address_delete, name="family-address-delete",
    ),
]
