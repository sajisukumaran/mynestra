"""Cards URLs — mounted under /t/<slug>/cards/ (member-accessible)."""

from django.urls import path

from apps.cards import views

app_name = "cards"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    # Credit cards
    path("credit/", views.credit_list, name="credit-list"),
    path("credit/new/", views.credit_create, name="credit-create"),
    path("credit/<int:pk>/", views.credit_detail, name="credit-detail"),
    path("credit/<int:pk>/edit/", views.credit_edit, name="credit-edit"),
    path("credit/<int:pk>/delete/", views.credit_delete, name="credit-delete"),
    path("credit/<int:pk>/txns/new/", views.txn_create, name="txn-create"),
    path("credit/<int:pk>/txns/<int:tx>/edit/", views.txn_edit, name="txn-edit"),
    path("credit/<int:pk>/txns/<int:tx>/delete/", views.txn_delete, name="txn-delete"),
    path("credit/<int:pk>/txns/<int:tx>/cleared/", views.txn_toggle_cleared, name="txn-cleared"),
    # Debit cards
    path("debit/", views.debit_list, name="debit-list"),
    path("debit/new/", views.debit_create, name="debit-create"),
    path("debit/<int:pk>/", views.debit_detail, name="debit-detail"),
    path("debit/<int:pk>/edit/", views.debit_edit, name="debit-edit"),
    path("debit/<int:pk>/delete/", views.debit_delete, name="debit-delete"),
    # htmx
    path("payee-search/", views.payee_search, name="payee-search"),
    path("holder-search/", views.holder_search, name="holder-search"),
]
