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
]
