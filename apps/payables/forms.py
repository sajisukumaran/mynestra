"""Payables forms — validation + error rendering; templates render inputs with cotton components
bound to these fields' values (the app's idiom)."""

from django import forms

from apps.payables.models import Item, PaymentTerm


class ItemForm(forms.ModelForm):
    """Create/edit a catalog item. Posting defaults (default_account, capitalize_default,
    asset_account) are parsed from POST in the view (account selects), the app's idiom."""

    class Meta:
        model = Item
        fields = ["name", "description", "upc", "kind", "unit", "notes", "is_active"]

    def clean_name(self):
        return self.cleaned_data["name"].strip()


class PaymentTermForm(forms.ModelForm):
    """Create/edit a custom payment term. `kind` drives which fields matter; `clean` enforces the
    per-kind requirements (net days, day-of-month, discount pair)."""

    class Meta:
        model = PaymentTerm
        fields = ["name", "kind", "net_days", "day_of_month", "discount_percent", "discount_days"]

    def clean_name(self):
        return self.cleaned_data["name"].strip()

    def clean(self):
        cleaned = super().clean()
        kind = cleaned.get("kind")
        if kind == PaymentTerm.Kind.NET_DAYS and not cleaned.get("net_days"):
            self.add_error("net_days", "Net terms need a number of days greater than zero.")
        if kind == PaymentTerm.Kind.DAY_OF_MONTH:
            dom = cleaned.get("day_of_month")
            if not dom or not (1 <= dom <= 31):
                self.add_error("day_of_month", "Choose a day of the month between 1 and 31.")
        pct = cleaned.get("discount_percent") or 0
        days = cleaned.get("discount_days") or 0
        if (pct > 0) != (days > 0):
            self.add_error(
                "discount_days",
                "An early-payment discount needs both a percentage and a number of days.",
            )
        return cleaned
