"""Health URLs — mounted under /t/<slug>/health/ (member-accessible)."""

from django.urls import path

from apps.health import views

app_name = "health"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("providers/", views.providers, name="providers"),

    # Visits (encounters)
    path("visits/", views.encounter_list, name="visits"),
    path("visits/new/", views.encounter_create, name="visit-create"),
    path("visits/<int:pk>/", views.encounter_detail, name="visit-detail"),
    path("visits/<int:pk>/edit/", views.encounter_edit, name="visit-edit"),
    path("visits/<int:pk>/delete/", views.encounter_delete, name="visit-delete"),
    path("visits/<int:pk>/invoices/new/", views.invoice_create_for_visit,
         name="visit-invoice-create"),
    path("visits/<int:pk>/documents/new/", views.encounter_document_upload,
         name="visit-document-upload"),

    # Provider invoices
    path("invoices/", views.invoice_list, name="invoices"),
    path("invoices/new/", views.invoice_create, name="invoice-create"),
    path("invoices/<int:pk>/", views.invoice_detail, name="invoice-detail"),
    path("invoices/<int:pk>/edit/", views.invoice_edit, name="invoice-edit"),
    path("invoices/<int:pk>/delete/", views.invoice_delete, name="invoice-delete"),
    path("invoices/<int:pk>/confirm/", views.invoice_confirm, name="invoice-confirm"),
    path("invoices/<int:pk>/pay/", views.invoice_pay, name="invoice-pay"),
    path("invoices/<int:pk>/payments/<int:pay>/delete/", views.invoice_payment_delete,
         name="invoice-payment-delete"),
    path("invoices/<int:pk>/writeoff/", views.invoice_writeoff, name="invoice-writeoff"),
    path("invoices/<int:pk>/dispute/", views.invoice_dispute, name="invoice-dispute"),
    path("invoices/<int:pk>/resolve/", views.invoice_resolve, name="invoice-resolve"),
    path("invoices/<int:pk>/refund/", views.invoice_refund, name="invoice-refund"),
    path("invoices/<int:pk>/documents/new/", views.invoice_document_upload,
         name="invoice-document-upload"),

    # Documents
    path("documents/<int:did>/delete/", views.document_delete, name="document-delete"),
]
