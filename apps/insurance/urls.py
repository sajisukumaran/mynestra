"""Insurance URLs — mounted under /t/<slug>/insurance/ (member-accessible)."""

from django.urls import path

from apps.insurance import views

app_name = "insurance"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("policies/", views.policy_list, name="list"),
    path("policies/new/", views.policy_create, name="policy-create"),
    path("policies/<int:pk>/", views.policy_detail, name="policy-detail"),
    path("policies/<int:pk>/edit/", views.policy_edit, name="policy-edit"),
    path("policies/<int:pk>/delete/", views.policy_delete, name="policy-delete"),
    path("policies/<int:pk>/premiums/new/", views.premium_create, name="premium-create"),
    path("policies/<int:pk>/premiums/<int:prem>/edit/", views.premium_edit, name="premium-edit"),
    path(
        "policies/<int:pk>/premiums/<int:prem>/delete/",
        views.premium_delete,
        name="premium-delete",
    ),
    path("insurer-search/", views.insurer_search, name="insurer-search"),
]
