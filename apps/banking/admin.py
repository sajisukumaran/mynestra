from django.contrib import admin

from .models import BankAccount, BankAccountHolder, BankTransaction


class HolderInline(admin.TabularInline):
    model = BankAccountHolder
    extra = 0


@admin.register(BankAccount)
class BankAccountAdmin(admin.ModelAdmin):
    list_display = ("nickname", "account_type", "bank", "currency", "is_active")
    list_filter = ("account_type", "is_active")
    search_fields = ("nickname", "number")
    inlines = [HolderInline]


@admin.register(BankTransaction)
class BankTransactionAdmin(admin.ModelAdmin):
    list_display = ("date", "account", "txn_type", "amount", "cleared")
    list_filter = ("txn_type", "cleared")
    date_hierarchy = "date"
