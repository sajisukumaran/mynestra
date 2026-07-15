"""Loans & Liabilities URLs — mounted under /t/<slug>/loans/ (member-accessible)."""

from django.urls import path

from apps.loans import views

app_name = "loans"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("all/", views.loan_list, name="list"),
    path("new/", views.loan_create, name="loan-create"),
    path("<int:pk>/", views.loan_detail, name="loan-detail"),
    path("<int:pk>/edit/", views.loan_edit, name="loan-edit"),
    path("<int:pk>/delete/", views.loan_delete, name="loan-delete"),
    path("<int:pk>/txns/new/", views.txn_create, name="txn-create"),
    path("<int:pk>/txns/<int:tx>/edit/", views.txn_edit, name="txn-edit"),
    path("<int:pk>/txns/<int:tx>/delete/", views.txn_delete, name="txn-delete"),
    path("<int:pk>/rate/", views.rate_add, name="rate-add"),
    path("<int:pk>/payoff-projection/", views.payoff_fragment, name="payoff-projection"),
    path("<int:pk>/payment-split/", views.payment_split_fragment, name="payment-split"),
    path("lender-search/", views.lender_search, name="lender-search"),
    path("borrower-search/", views.borrower_search, name="borrower-search"),
]
