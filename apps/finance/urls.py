"""Finance URLs — mounted under /t/<slug>/finance/ (member-accessible). Read-only for now."""

from django.urls import path

from apps.finance import views

app_name = "finance"

urlpatterns = [
    path("", views.finance_home, name="dashboard"),
]
