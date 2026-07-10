"""Banking forms. The account form is a light ModelForm for the free-text fields; the bank, branch,
currency, account-type selects (plus holders, lifecycle dates and the opening balance) are
hand-rendered selects parsed in the view — matching the app's htmx idiom (dependent branch select,
holder toggle-chips)."""

from django import forms

from apps.banking.models import BankAccount


class BankAccountForm(forms.ModelForm):
    class Meta:
        model = BankAccount
        fields = ["nickname", "number", "is_active", "notes"]
        widgets = {
            "nickname": forms.TextInput(attrs={"placeholder": "e.g. HDFC Salary"}),
            "number": forms.TextInput(attrs={"placeholder": "Account number"}),
        }
