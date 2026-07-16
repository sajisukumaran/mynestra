"""Admin for the Real Estate module (dev convenience; the app is driven by its own UI)."""

from django.contrib import admin

from apps.realestate.models import (
    Property,
    PropertyCostEvent,
    PropertyDisposal,
    PropertyDocument,
    PropertyOwner,
    PropertyValuation,
)


class PropertyOwnerInline(admin.TabularInline):
    model = PropertyOwner
    extra = 0


@admin.register(Property)
class PropertyAdmin(admin.ModelAdmin):
    list_display = ("nickname", "property_type", "use", "ownership_mode", "is_active")
    list_filter = ("property_type", "use", "ownership_mode", "is_active")
    search_fields = ("nickname", "address_line1", "city", "postal_code")
    inlines = [PropertyOwnerInline]


@admin.register(PropertyCostEvent)
class PropertyCostEventAdmin(admin.ModelAdmin):
    list_display = ("property", "kind", "date", "amount", "funding_source")
    list_filter = ("kind", "funding_source")
    date_hierarchy = "date"


@admin.register(PropertyDisposal)
class PropertyDisposalAdmin(admin.ModelAdmin):
    list_display = ("property", "method", "date", "proceeds")
    list_filter = ("method",)


@admin.register(PropertyDocument)
class PropertyDocumentAdmin(admin.ModelAdmin):
    list_display = ("property", "title", "doc_type", "created_at")
    list_filter = ("doc_type",)
    search_fields = ("title", "note")


admin.site.register(PropertyValuation)
