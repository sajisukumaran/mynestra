"""Automobile forms. A light ModelForm for the free-text identity fields; ownership, currency,
parties, terms, drivers, funding and cost events are hand-rendered and parsed in the view (matching
the app's htmx idiom, mirroring Loans)."""

from django import forms

from apps.automobile.models import Vehicle


class VehicleForm(forms.ModelForm):
    class Meta:
        model = Vehicle
        fields = [
            "nickname", "year", "make", "model_name", "trim", "body_type", "color",
            "vin", "license_plate", "plate_jurisdiction", "title_number",
            "insurance_carrier", "insurance_policy_number", "warranty_provider", "notes",
        ]
        widgets = {
            "nickname": forms.TextInput(attrs={"placeholder": "e.g. Family SUV"}),
            "make": forms.TextInput(attrs={"placeholder": "e.g. Toyota"}),
            "model_name": forms.TextInput(attrs={"placeholder": "e.g. Highlander"}),
        }
