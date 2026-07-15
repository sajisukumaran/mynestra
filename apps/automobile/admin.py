"""Admin for the Automobile module (dev convenience; the app is driven by its own UI)."""

from django.contrib import admin

from apps.automobile.models import (
    OdometerReading,
    ServiceSchedule,
    Vehicle,
    VehicleCostEvent,
    VehicleDisposal,
    VehicleDriver,
    VehicleValuation,
)


class VehicleDriverInline(admin.TabularInline):
    model = VehicleDriver
    extra = 0


class ServiceScheduleInline(admin.TabularInline):
    model = ServiceSchedule
    extra = 0


@admin.register(Vehicle)
class VehicleAdmin(admin.ModelAdmin):
    list_display = ("nickname", "year", "make", "model_name", "ownership_mode", "is_active")
    list_filter = ("ownership_mode", "fuel_type", "is_active")
    search_fields = ("nickname", "make", "model_name", "vin", "license_plate")
    inlines = [VehicleDriverInline, ServiceScheduleInline]


@admin.register(VehicleCostEvent)
class VehicleCostEventAdmin(admin.ModelAdmin):
    list_display = ("vehicle", "kind", "date", "amount", "funding_source")
    list_filter = ("kind", "funding_source")
    date_hierarchy = "date"


@admin.register(VehicleDisposal)
class VehicleDisposalAdmin(admin.ModelAdmin):
    list_display = ("vehicle", "method", "date", "proceeds")
    list_filter = ("method",)


admin.site.register(VehicleValuation)
admin.site.register(OdometerReading)
