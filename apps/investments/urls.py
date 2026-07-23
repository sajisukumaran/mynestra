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
    path("accounts/<int:pk>/register.csv", views.account_register_csv, name="register-csv"),
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
    # Institutions (brokerages — grouped view over accounts)
    path("institutions/", views.institution_list, name="institutions"),
    path("institutions/new/", views.institution_create, name="institution-create"),
    path("institutions/<int:org>/", views.institution_detail, name="institution-detail"),
    path("institutions/<int:org>/edit/", views.institution_edit, name="institution-edit"),
    path(
        "institutions/<int:org>/branches/new/",
        views.branch_create,
        name="institution-branch-create",
    ),
    # Securities (instrument master)
    path("securities/", views.security_list, name="securities"),
    path("securities/mass-prices/", views.security_mass_price, name="security-mass-price"),
    path("securities/new/", views.security_create, name="security-create"),
    path("securities/<int:pk>/", views.security_detail, name="security-detail"),
    path("securities/<int:pk>/edit/", views.security_edit, name="security-edit"),
    path("securities/<int:pk>/price/", views.security_price, name="security-price"),
    path(
        "securities/<int:pk>/price/<int:price_id>/edit/",
        views.security_price_edit,
        name="security-price-edit",
    ),
    path(
        "securities/<int:pk>/price/<int:price_id>/delete/",
        views.security_price_delete,
        name="security-price-delete",
    ),
    path("securities/<int:pk>/rename/", views.security_rename, name="security-rename"),
    path("securities/<int:pk>/delete/", views.security_delete, name="security-delete"),
    # htmx fragments
    path("value-over-time/", views.value_over_time_fragment, name="value-over-time"),
    path("payee-search/", views.payee_search, name="payee-search"),
    path("accounts/holder-search/", views.holder_search, name="holder-search"),
    path("branch-options/", views.branch_options, name="branch-options"),
]
