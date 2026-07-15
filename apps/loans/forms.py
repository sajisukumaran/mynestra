"""Loans forms. A light ModelForm for the free-text fields; the loan type, lender, currency,
terms, borrowers, net-worth toggle and opening balance are hand-rendered and parsed in the view
(matching the app's htmx idiom)."""

from django import forms

from apps.loans.models import Loan


class LoanForm(forms.ModelForm):
    class Meta:
        model = Loan
        fields = ["nickname", "account_number", "is_active", "notes"]
        widgets = {
            "nickname": forms.TextInput(attrs={"placeholder": "e.g. Home mortgage"}),
            "account_number": forms.TextInput(attrs={"placeholder": "Loan / account number"}),
        }
