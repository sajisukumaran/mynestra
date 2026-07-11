"""Investments URLs — mounted under /t/<slug>/investments/ (member-accessible)."""

from django.urls import path

from apps.investments import views

app_name = "investments"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    # Accounts
    path("accounts/", views.account_list, name="list"),
    path("accounts/new/", views.account_create, name="account-create"),
    path("accounts/<int:pk>/", views.account_detail, name="account-detail"),
    path("accounts/<int:pk>/edit/", views.account_edit, name="account-edit"),
    path("accounts/<int:pk>/delete/", views.account_delete, name="account-delete"),
    path("accounts/<int:pk>/holdings/<int:sec>/", views.holding_detail, name="holding-detail"),
    path("accounts/<int:pk>/txns/new/", views.txn_create, name="txn-create"),
    path("accounts/<int:pk>/txns/<int:tx>/edit/", views.txn_edit, name="txn-edit"),
    path("accounts/<int:pk>/txns/<int:tx>/delete/", views.txn_delete, name="txn-delete"),
    path("accounts/<int:pk>/txns/<int:tx>/cleared/", views.txn_toggle_cleared, name="txn-cleared"),
    # Vesting (employer match & equity grants)
    path("accounts/<int:pk>/vesting/new/", views.vesting_create, name="vesting-create"),
    path("accounts/<int:pk>/vesting/<int:vid>/edit/", views.vesting_edit, name="vesting-edit"),
    path(
        "accounts/<int:pk>/vesting/<int:vid>/delete/",
        views.vesting_delete,
        name="vesting-delete",
    ),
    # Securities (instrument master)
    path("securities/", views.security_list, name="securities"),
    path("securities/new/", views.security_create, name="security-create"),
    path("securities/<int:pk>/", views.security_detail, name="security-detail"),
    path("securities/<int:pk>/edit/", views.security_edit, name="security-edit"),
    path("securities/<int:pk>/price/", views.security_price, name="security-price"),
    path("securities/<int:pk>/delete/", views.security_delete, name="security-delete"),
    # htmx fragments
    path("payee-search/", views.payee_search, name="payee-search"),
    path("accounts/holder-search/", views.holder_search, name="holder-search"),
    path("branch-options/", views.branch_options, name="branch-options"),
]
