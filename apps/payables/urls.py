"""Payables URLs — mounted under /t/<slug>/payables/ (member-accessible)."""

from django.urls import path

from apps.payables import views

app_name = "payables"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    # Payments (funding-integrated; allocate across a vendor's bills)
    path("payments/", views.payment_list, name="payments"),
    path("payments/new/", views.payment_create, name="payment-create"),
    path("payments/<int:pk>/edit/", views.payment_edit, name="payment-edit"),
    path("payments/<int:pk>/delete/", views.payment_delete, name="payment-delete"),
    # Bills (accrual accounts-payable documents)
    path("bills/", views.bill_list, name="bills"),
    path("bills/new/", views.bill_create, name="bill-create"),
    path("bills/<int:pk>/", views.bill_detail, name="bill-detail"),
    path("bills/<int:pk>/edit/", views.bill_edit, name="bill-edit"),
    path("bills/<int:pk>/void/", views.bill_void, name="bill-void"),
    path("bills/<int:pk>/delete/", views.bill_delete, name="bill-delete"),
    # Items (catalog master)
    path("items/", views.item_list, name="items"),
    path("items/new/", views.item_create, name="item-create"),
    path("items/<int:pk>/", views.item_detail, name="item-detail"),
    path("items/<int:pk>/edit/", views.item_edit, name="item-edit"),
    path("items/<int:pk>/delete/", views.item_delete, name="item-delete"),
    path("items/<int:pk>/skus/new/", views.sku_add, name="sku-add"),
    path("items/<int:pk>/skus/<int:sku_id>/edit/", views.sku_edit, name="sku-edit"),
    path("items/<int:pk>/skus/<int:sku_id>/delete/", views.sku_delete, name="sku-delete"),
    # Vendors
    path("vendors/", views.vendor_list, name="vendors"),
    path("vendors/new/", views.vendor_create, name="vendor-create"),
    path("vendors/<int:pk>/", views.vendor_detail, name="vendor-detail"),
    path("vendors/<int:pk>/edit/", views.vendor_edit, name="vendor-edit"),
    path("vendors/<int:pk>/delete/", views.vendor_delete, name="vendor-delete"),
    # htmx fragments
    path("item-search/", views.item_search, name="item-search"),
    path("vendor-search/", views.vendor_search, name="vendor-search"),
]
