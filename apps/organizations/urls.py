"""Organizations URLs — mounted under /t/<slug>/organizations/ (member-accessible)."""

from django.urls import path

from apps.organizations import views

app_name = "organizations"

urlpatterns = [
    path("", views.org_list, name="list"),
    path("new/", views.org_create, name="org-create"),
    path("<int:pk>/", views.org_detail, name="org-detail"),
    path("<int:pk>/edit/", views.org_edit, name="org-edit"),
    path("<int:pk>/delete/", views.org_delete, name="org-delete"),
    path("<int:pk>/addresses/new/", views.org_address_create, name="org-address-create"),
    path(
        "<int:pk>/addresses/<int:addr_pk>/edit/",
        views.org_address_edit, name="org-address-edit",
    ),
    path(
        "<int:pk>/addresses/<int:addr_pk>/delete/",
        views.org_address_delete, name="org-address-delete",
    ),
    # Branches (+ their own channels/addresses)
    path("<int:pk>/branches/new/", views.branch_create, name="branch-create"),
    path("<int:pk>/branches/<int:branch_pk>/edit/", views.branch_edit, name="branch-edit"),
    path("<int:pk>/branches/<int:branch_pk>/delete/", views.branch_delete, name="branch-delete"),
    path(
        "<int:pk>/branches/<int:branch_pk>/channels/new/",
        views.branch_channel_create, name="branch-channel-create",
    ),
    path(
        "<int:pk>/branches/<int:branch_pk>/channels/<int:ch_pk>/edit/",
        views.branch_channel_edit, name="branch-channel-edit",
    ),
    path(
        "<int:pk>/branches/<int:branch_pk>/channels/<int:ch_pk>/delete/",
        views.branch_channel_delete, name="branch-channel-delete",
    ),
    path(
        "<int:pk>/branches/<int:branch_pk>/addresses/new/",
        views.branch_address_create, name="branch-address-create",
    ),
    path(
        "<int:pk>/branches/<int:branch_pk>/addresses/<int:addr_pk>/edit/",
        views.branch_address_edit, name="branch-address-edit",
    ),
    path(
        "<int:pk>/branches/<int:branch_pk>/addresses/<int:addr_pk>/delete/",
        views.branch_address_delete, name="branch-address-delete",
    ),
    # Key people (P2O)
    path("<int:pk>/people/search/", views.org_person_search, name="org-person-search"),
    path("<int:pk>/people/new/", views.p2o_create, name="p2o-create"),
    path("<int:pk>/people/<int:link_pk>/edit/", views.p2o_edit, name="p2o-edit"),
    path("<int:pk>/people/<int:link_pk>/delete/", views.p2o_delete, name="p2o-delete"),
]
