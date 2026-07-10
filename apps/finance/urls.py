"""Finance URLs — mounted under /t/<slug>/finance/. Expert-mode only (404 in Standard). The chart
is member-viewable; the editor routes are Owner-only."""

from django.urls import path

from apps.finance import views

app_name = "finance"

urlpatterns = [
    path("", views.finance_home, name="dashboard"),
    path("accounts/new/", views.account_create, name="account-create"),
    path("accounts/<int:pk>/edit/", views.account_edit, name="account-edit"),
    path("accounts/<int:pk>/delete/", views.account_delete, name="account-delete"),
]
