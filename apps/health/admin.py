"""Admin for the Health module (dev convenience; the app is driven by its own UI)."""

from django.contrib import admin

from apps.health.models import (
    ClaimLine,
    ClaimRemark,
    CopayRule,
    Encounter,
    EncounterProvider,
    HealthDocument,
    HealthPlan,
    InvoiceCharge,
    MedicalClaim,
    Prescription,
    ProviderInvoice,
)


class EncounterProviderInline(admin.TabularInline):
    model = EncounterProvider
    extra = 0


class CopayRuleInline(admin.TabularInline):
    model = CopayRule
    extra = 0


@admin.register(HealthPlan)
class HealthPlanAdmin(admin.ModelAdmin):
    list_display = ("policy", "deductible_individual", "deductible_family", "oop_max_individual")
    inlines = [CopayRuleInline]


class InvoiceChargeInline(admin.TabularInline):
    model = InvoiceCharge
    extra = 0


@admin.register(Encounter)
class EncounterAdmin(admin.ModelAdmin):
    list_display = ("display", "patient", "encounter_type", "visit_status", "date")
    list_filter = ("encounter_type", "visit_status", "setting")
    date_hierarchy = "date"
    inlines = [EncounterProviderInline]


@admin.register(ProviderInvoice)
class ProviderInvoiceAdmin(admin.ModelAdmin):
    list_display = ("invoice_number", "biller_name", "invoice_date", "status", "amount_due")
    list_filter = ("status",)
    date_hierarchy = "invoice_date"
    inlines = [InvoiceChargeInline]


@admin.register(Prescription)
class PrescriptionAdmin(admin.ModelAdmin):
    list_display = ("drug_name", "patient", "pharmacy_name", "date", "status", "cost",
                    "refills_remaining", "next_refill_date")
    list_filter = ("status",)
    date_hierarchy = "date"


class ClaimLineInline(admin.TabularInline):
    model = ClaimLine
    extra = 0


class ClaimRemarkInline(admin.TabularInline):
    model = ClaimRemark
    extra = 0


@admin.register(MedicalClaim)
class MedicalClaimAdmin(admin.ModelAdmin):
    list_display = ("claim_number", "patient", "provider_name", "service_date", "status",
                    "total_billed", "total_plan_paid", "total_patient_responsibility")
    list_filter = ("status", "network")
    date_hierarchy = "service_date"
    inlines = [ClaimLineInline, ClaimRemarkInline]


@admin.register(HealthDocument)
class HealthDocumentAdmin(admin.ModelAdmin):
    list_display = ("title", "doc_type", "created_at")
    list_filter = ("doc_type",)
