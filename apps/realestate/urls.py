"""Real Estate URLs — mounted under /t/<slug>/realestate/ (member-accessible)."""

from django.urls import path

from apps.realestate import views

app_name = "realestate"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("all/", views.property_list, name="list"),
    path("new/", views.property_create, name="property-create"),
    path("<int:pk>/", views.property_detail, name="property-detail"),
    path("<int:pk>/edit/", views.property_edit, name="property-edit"),
    path("<int:pk>/delete/", views.property_delete, name="property-delete"),
    path("<int:pk>/costs/new/", views.cost_create, name="cost-create"),
    path("<int:pk>/costs/<int:ev>/edit/", views.cost_edit, name="cost-edit"),
    path("<int:pk>/costs/<int:ev>/delete/", views.cost_delete, name="cost-delete"),
    path("<int:pk>/valuation/", views.valuation_add, name="valuation-add"),
    path("<int:pk>/valuation/<int:vid>/delete/", views.valuation_delete, name="valuation-delete"),
    path("<int:pk>/dispose/", views.dispose, name="dispose"),
    path("org-search/", views.org_search, name="org-search"),
]
