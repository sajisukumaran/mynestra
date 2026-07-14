"""Investments forms. Light ModelForms for free-text fields; the institution/branch/currency/
registration selects (plus holders, lifecycle dates and opening balances) are hand-rendered and
parsed in the view — matching the app's htmx idiom (see banking)."""

from django import forms

from apps.investments.models import ContributionLimit, InvestmentAccount, Security


class InvestmentAccountForm(forms.ModelForm):
    class Meta:
        model = InvestmentAccount
        fields = ["nickname", "number", "is_active", "notes"]
        widgets = {
            "nickname": forms.TextInput(attrs={"placeholder": "e.g. Fidelity Individual"}),
            "number": forms.TextInput(attrs={"placeholder": "Account number"}),
        }


class ContributionLimitForm(forms.ModelForm):
    """A tax year's editable IRS IRA/HSA limits (managed in Setup). tax_year is unique — the
    ModelForm reports a friendly error on a duplicate; amounts must be non-negative."""

    class Meta:
        model = ContributionLimit
        fields = ["tax_year", "ira", "ira_catchup", "hsa_self", "hsa_family", "hsa_catchup"]

    def clean_tax_year(self):
        year = self.cleaned_data["tax_year"]
        if year < 1970 or year > 2200:
            raise forms.ValidationError("Enter a valid tax year.")
        return year

    def clean(self):
        cleaned = super().clean()
        for f in ("ira", "ira_catchup", "hsa_self", "hsa_family", "hsa_catchup"):
            v = cleaned.get(f)
            if v is not None and v < 0:
                self.add_error(f, "Cannot be negative.")
        return cleaned


class SecurityForm(forms.ModelForm):
    class Meta:
        model = Security
        fields = ["symbol", "name", "kind", "asset_class", "apr", "maturity_date",
                  "underlying", "option_right", "strike", "expiration",
                  "track_lots", "track_price", "is_active", "notes"]
        widgets = {
            "symbol": forms.TextInput(attrs={"placeholder": "e.g. VTI"}),
            "name": forms.TextInput(attrs={"placeholder": "e.g. Vanguard Total Stock Market ETF"}),
            "maturity_date": forms.DateInput(attrs={"type": "date"}),
            "apr": forms.NumberInput(attrs={"step": "0.01", "placeholder": "e.g. 5.25"}),
            "strike": forms.NumberInput(attrs={"step": "0.01", "placeholder": "e.g. 250"}),
            "expiration": forms.DateInput(attrs={"type": "date"}),
        }
