"""Health forms. Light ModelForms for the free-text identity fields; type / status / parties /
dates / charges / roster / funding are hand-rendered and parsed in the view (the app's htmx idiom,
mirroring Insurance / Real Estate)."""

from django import forms

from apps.health.models import Encounter, ProviderInvoice


class EncounterForm(forms.ModelForm):
    class Meta:
        model = Encounter
        fields = ["reason", "notes"]
        widgets = {
            "reason": forms.TextInput(attrs={"placeholder": "e.g. Annual physical / knee pain"}),
        }


class InvoiceForm(forms.ModelForm):
    class Meta:
        model = ProviderInvoice
        fields = ["invoice_number", "reference", "memo"]
        widgets = {
            "invoice_number": forms.TextInput(attrs={"placeholder": "Statement #"}),
        }
