"""Admin for the Loans & Liabilities module (dev convenience; the app is driven by its own UI)."""

from django.contrib import admin

from apps.loans.models import Loan, LoanBorrower, LoanRateChange, LoanTransaction


class LoanBorrowerInline(admin.TabularInline):
    model = LoanBorrower
    extra = 0


class LoanRateChangeInline(admin.TabularInline):
    model = LoanRateChange
    extra = 0


@admin.register(Loan)
class LoanAdmin(admin.ModelAdmin):
    list_display = ("nickname", "loan_type", "annual_rate", "counts_toward_net_worth", "is_active")
    list_filter = ("loan_type", "rate_type", "counts_toward_net_worth", "is_active")
    search_fields = ("nickname", "account_number")
    inlines = [LoanBorrowerInline, LoanRateChangeInline]


@admin.register(LoanTransaction)
class LoanTransactionAdmin(admin.ModelAdmin):
    list_display = ("loan", "txn_type", "date", "amount", "funding_source", "cleared")
    list_filter = ("txn_type", "funding_source", "cleared")
    date_hierarchy = "date"
