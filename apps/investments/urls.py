"""Investments URLs — mounted under /t/<slug>/investments/ (member-accessible)."""

from django.urls import path

from apps.investments import views

app_name = "investments"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
]
