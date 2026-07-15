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
    # Registration / inspection / property-tax records.
    path("<int:pk>/registrations/new/", views.registration_add, name="registration-add"),
    path("<int:pk>/registrations/<int:rid>/edit/", views.registration_edit,
         name="registration-edit"),
    path("<int:pk>/registrations/<int:rid>/delete/", views.registration_delete,
         name="registration-delete"),
    path("<int:pk>/inspections/new/", views.inspection_add, name="inspection-add"),
    path("<int:pk>/inspections/<int:iid>/edit/", views.inspection_edit, name="inspection-edit"),
    path("<int:pk>/inspections/<int:iid>/delete/", views.inspection_delete,
         name="inspection-delete"),
    path("<int:pk>/property-taxes/new/", views.property_tax_add, name="property-tax-add"),
    path("<int:pk>/property-taxes/<int:tid>/edit/", views.property_tax_edit,
         name="property-tax-edit"),
    path("<int:pk>/property-taxes/<int:tid>/delete/", views.property_tax_delete,
         name="property-tax-delete"),
    path("<int:pk>/title-release/", views.title_release, name="title-release"),
    # Multi-line service invoices.
    path("<int:pk>/service-invoices/new/", views.service_invoice_add, name="service-invoice-add"),
    path("<int:pk>/service-invoices/<int:sid>/edit/", views.service_invoice_edit,
         name="service-invoice-edit"),
    path("<int:pk>/service-invoices/<int:sid>/delete/", views.service_invoice_delete,
         name="service-invoice-delete"),
    path("<int:pk>/valuation/", views.valuation_add, name="valuation-add"),
    path("<int:pk>/odometer/", views.odometer_add, name="odometer-add"),
    path("<int:pk>/service/new/", views.service_add, name="service-add"),
    path("<int:pk>/service/<int:sid>/edit/", views.service_edit, name="service-edit"),
    path("<int:pk>/dispose/", views.dispose, name="dispose"),
    path("<int:pk>/value-chart/", views.value_chart_fragment, name="value-chart"),
    path("vendor-search/", views.vendor_search, name="vendor-search"),
    path("driver-search/", views.driver_search, name="driver-search"),
]
