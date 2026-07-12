"""Investments forms. Light ModelForms for free-text fields; the institution/branch/currency/
registration selects (plus holders, lifecycle dates and opening balances) are hand-rendered and
parsed in the view — matching the app's htmx idiom (see banking)."""

from django import forms

from apps.investments.models import InvestmentAccount, Security


class InvestmentAccountForm(forms.ModelForm):
    class Meta:
        model = InvestmentAccount
        fields = ["nickname", "number", "is_active", "notes"]
        widgets = {
            "nickname": forms.TextInput(attrs={"placeholder": "e.g. Fidelity Individual"}),
            "number": forms.TextInput(attrs={"placeholder": "Account number"}),
        }


class SecurityForm(forms.ModelForm):
    class Meta:
        model = Security
        fields = ["symbol", "name", "kind", "asset_class", "apr", "maturity_date",
                  "underlying", "option_right", "strike", "expiration",
                  "track_lots", "is_active", "notes"]
        widgets = {
            "symbol": forms.TextInput(attrs={"placeholder": "e.g. VTI"}),
            "name": forms.TextInput(attrs={"placeholder": "e.g. Vanguard Total Stock Market ETF"}),
            "maturity_date": forms.DateInput(attrs={"type": "date"}),
            "apr": forms.NumberInput(attrs={"step": "0.01", "placeholder": "e.g. 5.25"}),
            "strike": forms.NumberInput(attrs={"step": "0.01", "placeholder": "e.g. 250"}),
            "expiration": forms.DateInput(attrs={"type": "date"}),
        }
