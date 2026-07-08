"""Contacts URLs — mounted under /t/<slug>/contacts/ (member-accessible)."""

from django.urls import path

from apps.contacts import views

app_name = "contacts"

urlpatterns = [
    path("people/", views.people_list, name="people"),
]
