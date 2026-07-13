"""Payables URLs — mounted under /t/<slug>/payables/ (member-accessible)."""

from django.urls import path

from apps.payables import views

app_name = "payables"

urlpatterns = [
    path("", views.payables_home, name="home"),
    # Items (catalog master)
    path("items/", views.item_list, name="items"),
    path("items/new/", views.item_create, name="item-create"),
    path("items/<int:pk>/", views.item_detail, name="item-detail"),
    path("items/<int:pk>/edit/", views.item_edit, name="item-edit"),
    path("items/<int:pk>/delete/", views.item_delete, name="item-delete"),
    path("items/<int:pk>/skus/new/", views.sku_add, name="sku-add"),
    path("items/<int:pk>/skus/<int:sku_id>/edit/", views.sku_edit, name="sku-edit"),
    path("items/<int:pk>/skus/<int:sku_id>/delete/", views.sku_delete, name="sku-delete"),
    # htmx fragments
    path("item-search/", views.item_search, name="item-search"),
]
