"""Admin for the Insurance module (dev convenience; the app is driven by its own UI)."""

from django.contrib import admin

from apps.insurance.models import (
    InsurancePolicy,
    InsurancePremium,
    PolicyCoverage,
    PolicyMember,
)


class PolicyCoverageInline(admin.TabularInline):
    model = PolicyCoverage
    extra = 0


class PolicyMemberInline(admin.TabularInline):
    model = PolicyMember
    extra = 0


@admin.register(InsurancePolicy)
class InsurancePolicyAdmin(admin.ModelAdmin):
    list_display = ("display", "policy_type", "status", "premium_amount", "expiry_date")
    list_filter = ("policy_type", "status")
    search_fields = ("nickname", "plan_name", "policy_number")
    inlines = [PolicyCoverageInline, PolicyMemberInline]


@admin.register(InsurancePremium)
class InsurancePremiumAdmin(admin.ModelAdmin):
    list_display = ("policy", "date", "amount", "funding_source")
    list_filter = ("funding_source",)
    date_hierarchy = "date"
