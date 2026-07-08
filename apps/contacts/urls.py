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
]
