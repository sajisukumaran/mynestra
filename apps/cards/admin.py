"""Admin for the Cards module (dev convenience; the app is driven by its own tenant UI)."""

from django.contrib import admin

from apps.cards.models import (
    CreditCard,
    CreditCardHolder,
    CreditCardTransaction,
    DebitCard,
)


class CreditCardHolderInline(admin.TabularInline):
    model = CreditCardHolder
    extra = 0


@admin.register(CreditCard)
class CreditCardAdmin(admin.ModelAdmin):
    list_display = ("nickname", "issuer", "network", "credit_limit", "is_active")
    list_filter = ("network", "is_active")
    search_fields = ("nickname", "number", "issuer__name")
    inlines = [CreditCardHolderInline]


@admin.register(CreditCardTransaction)
class CreditCardTransactionAdmin(admin.ModelAdmin):
    list_display = ("card", "txn_type", "date", "amount", "cleared")
    list_filter = ("txn_type", "cleared")
    date_hierarchy = "date"


@admin.register(DebitCard)
class DebitCardAdmin(admin.ModelAdmin):
    list_display = ("nickname", "bank_account", "network", "holder", "is_active")
    list_filter = ("network", "is_active")
    search_fields = ("nickname", "number")
