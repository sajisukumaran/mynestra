"""Automobile (Vehicles) URLs — mounted under /t/<slug>/automobile/ (member-accessible)."""

from django.urls import path

from apps.automobile import views

app_name = "automobile"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("all/", views.vehicle_list, name="list"),
    path("new/", views.vehicle_create, name="vehicle-create"),
    path("insurance/new/", views.insurance_create, name="insurance-create"),
    path("<int:pk>/", views.vehicle_detail, name="vehicle-detail"),
    path("<int:pk>/edit/", views.vehicle_edit, name="vehicle-edit"),
    path("<int:pk>/delete/", views.vehicle_delete, name="vehicle-delete"),
    path("<int:pk>/costs/new/", views.cost_create, name="cost-create"),
    path("<int:pk>/costs/<int:ev>/edit/", views.cost_edit, name="cost-edit"),
    path("<int:pk>/costs/<int:ev>/delete/", views.cost_delete, name="cost-delete"),
    path("<int:pk>/valuation/", views.valuation_add, name="valuation-add"),
    path("<int:pk>/odometer/", views.odometer_add, name="odometer-add"),
    path("<int:pk>/service/new/", views.service_add, name="service-add"),
    path("<int:pk>/service/<int:sid>/edit/", views.service_edit, name="service-edit"),
    path("<int:pk>/dispose/", views.dispose, name="dispose"),
    path("<int:pk>/value-chart/", views.value_chart_fragment, name="value-chart"),
    path("vendor-search/", views.vendor_search, name="vendor-search"),
    path("driver-search/", views.driver_search, name="driver-search"),
]
