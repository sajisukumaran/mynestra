"""Banking URLs — mounted under /t/<slug>/banking/ (member-accessible)."""

from django.urls import path

from apps.banking import views

app_name = "banking"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("accounts/", views.account_list, name="list"),
    path("accounts/new/", views.account_create, name="account-create"),
    path("accounts/<int:pk>/", views.account_detail, name="account-detail"),
    path("accounts/<int:pk>/edit/", views.account_edit, name="account-edit"),
    path("accounts/<int:pk>/delete/", views.account_delete, name="account-delete"),
    path("accounts/<int:pk>/txns/new/", views.txn_create, name="txn-create"),
    path("accounts/<int:pk>/txns/<int:tx>/edit/", views.txn_edit, name="txn-edit"),
    path("accounts/<int:pk>/txns/<int:tx>/delete/", views.txn_delete, name="txn-delete"),
    path("accounts/<int:pk>/txns/<int:tx>/cleared/", views.txn_toggle_cleared, name="txn-cleared"),
    path("payee-search/", views.payee_search, name="payee-search"),
    path("accounts/holder-search/", views.holder_search, name="holder-search"),
    path("branch-options/", views.branch_options, name="branch-options"),
]
