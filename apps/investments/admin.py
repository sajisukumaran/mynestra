from django.contrib import admin

from .models import (
    InvestmentAccount,
    InvestmentAccountHolder,
    InvestmentTransaction,
    Lot,
    Security,
    SecurityPrice,
)


class HolderInline(admin.TabularInline):
    model = InvestmentAccountHolder
    extra = 0


class PriceInline(admin.TabularInline):
    model = SecurityPrice
    extra = 0


@admin.register(Security)
class SecurityAdmin(admin.ModelAdmin):
    list_display = ("symbol", "name", "kind", "asset_class", "currency", "is_active")
    list_filter = ("kind", "asset_class", "is_active")
    search_fields = ("symbol", "name")
    inlines = [PriceInline]


@admin.register(InvestmentAccount)
class InvestmentAccountAdmin(admin.ModelAdmin):
    list_display = ("nickname", "registration", "institution", "currency", "is_active")
    list_filter = ("registration", "is_active")
    search_fields = ("nickname", "number")
    inlines = [HolderInline]


@admin.register(InvestmentTransaction)
class InvestmentTransactionAdmin(admin.ModelAdmin):
    list_display = ("date", "account", "txn_type", "security", "quantity", "amount", "cleared")
    list_filter = ("txn_type", "cleared")
    date_hierarchy = "date"


@admin.register(Lot)
class LotAdmin(admin.ModelAdmin):
    list_display = ("account", "security", "acquired_date", "remaining_quantity", "cost_basis", "open")
    list_filter = ("open",)
